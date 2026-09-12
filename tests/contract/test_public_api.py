"""Public-API contract — guards what consumers can import."""

import dataclasses

import pytest

import stromy_asset_transport


@pytest.mark.contract
def test_all_is_a_list() -> None:
    assert isinstance(stromy_asset_transport.__all__, list)


@pytest.mark.contract
def test_all_symbols_are_exported() -> None:
    for symbol in stromy_asset_transport.__all__:
        assert hasattr(stromy_asset_transport, symbol), f"__all__ lists {symbol!r} but it's not exported"


@pytest.mark.contract
def test_core_public_api_present() -> None:
    """The two transport primitives + result type every consumer imports."""
    for symbol in ("AssetStore", "deliver_artifact", "DeliveryResult", "tenant_key"):
        assert symbol in stromy_asset_transport.__all__
        assert hasattr(stromy_asset_transport, symbol)


@pytest.mark.contract
def test_sharepoint_lock_public_api_present() -> None:
    for symbol in ("GraphRequestError", "SharePointLockedError"):
        assert symbol in stromy_asset_transport.__all__
        assert hasattr(stromy_asset_transport, symbol)


@pytest.mark.contract
def test_workspace_storage_public_api_present() -> None:
    """The read/list/create-only surface the format MCP's workspace memory binds to."""
    for symbol in (
        "SharePointFileRef",
        "DriveItemInfo",
        "get_file_metadata",
        "read_file",
        "list_children",
        "ensure_folder",
        "create_file_once",
        "WorkspaceStorageError",
        "TargetNotAllowed",
        "FileNotFound",
        "UnsafePath",
        "IdempotencyCollision",
        "DEFAULT_READ_MAX_BYTES",
        "LIST_CHILDREN_MAX_LIMIT",
    ):
        assert symbol in stromy_asset_transport.__all__
        assert hasattr(stromy_asset_transport, symbol)


@pytest.mark.contract
def test_the_ledger_stays_create_only() -> None:
    """Guard the create-only LEDGER contract at the API boundary.

    An immutable, content-addressed record makes a retry a no-op rather than a
    race, so there is deliberately no general `update_file`/`replace_file` here.
    That is a design choice. This test used to justify it by asserting the
    platform could not write conditionally at all — which is false (Graph honours
    `If-Match`; see the co-edit guard below) and is the premise ORG-186 was
    mis-designed around for five weeks.
    """
    for symbol in ("update_file", "replace_file", "put_content_if_match"):
        assert not hasattr(stromy_asset_transport, symbol)


@pytest.mark.contract
def test_coedit_guard_public_api_present() -> None:
    """ORG-PLAN-186 C6: the exact surface ORG-PLAN-280 Lane D binds to."""
    for symbol in ("StaleBaseConflict", "CONFLICT_REASONS", "DEFAULT_COMPARE_READ_MAX_BYTES"):
        assert symbol in stromy_asset_transport.__all__
        assert hasattr(stromy_asset_transport, symbol)


@pytest.mark.contract
def test_the_guard_args_are_keyword_only_with_preserving_defaults() -> None:
    """Positional additions would break `media-gen-mcp`'s call and every test fake
    that patches `deliver_artifact`; defaults keep an un-updated caller working."""
    import inspect

    sig = inspect.signature(stromy_asset_transport.deliver_artifact)
    for name in ("base_version", "base_sha256", "force"):
        param = sig.parameters[name]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{name} must be keyword-only"
    assert sig.parameters["base_version"].default is None
    assert sig.parameters["base_sha256"].default is None
    assert sig.parameters["force"].default is False


@pytest.mark.contract
def test_delivery_result_carries_the_guard_fields() -> None:
    fields = {f.name for f in dataclasses.fields(stromy_asset_transport.DeliveryResult)}
    assert {"conflict", "forced", "base_version_absent", "if_match_retried"} <= fields
