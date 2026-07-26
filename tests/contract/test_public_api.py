"""Public-API contract — guards what consumers can import."""

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
def test_no_conditional_content_replacement_is_exported() -> None:
    """Guard the create-only contract at the API boundary.

    Graph's supported small-file upload endpoint documents create-or-replace and
    no conditional content header, so an `update_file`/`If-Match` surface here
    would advertise a guarantee the platform does not make. Its absence is the
    design, not an omission — the ledger is immutable instead.
    """
    for symbol in ("update_file", "replace_file", "put_content_if_match"):
        assert not hasattr(stromy_asset_transport, symbol)
