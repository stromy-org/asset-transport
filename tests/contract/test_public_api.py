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
