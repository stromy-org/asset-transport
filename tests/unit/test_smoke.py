"""Smoke test — exercises module import and version."""

import pytest

import stromy_asset_transport


@pytest.mark.unit
def test_module_imports() -> None:
    assert stromy_asset_transport.__name__ == "stromy_asset_transport"


@pytest.mark.unit
def test_module_has_version() -> None:
    assert hasattr(stromy_asset_transport, "__version__")
    assert isinstance(stromy_asset_transport.__version__, str)
