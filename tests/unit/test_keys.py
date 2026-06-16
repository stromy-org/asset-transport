"""tenant_key unit tests."""

from __future__ import annotations

import hashlib

import pytest

from stromy_asset_transport import tenant_key


@pytest.mark.unit
def test_none_tenant_returns_bare_sha():
    sha = hashlib.sha256(b"x").hexdigest()
    assert tenant_key(None, sha) == sha


@pytest.mark.unit
def test_empty_tenant_returns_bare_sha():
    sha = hashlib.sha256(b"x").hexdigest()
    assert tenant_key("", sha) == sha


@pytest.mark.unit
def test_tenant_prefixes_sha():
    sha = hashlib.sha256(b"x").hexdigest()
    assert tenant_key("rebecaelmudesi", sha) == f"rebecaelmudesi/{sha}"


@pytest.mark.unit
def test_sha_is_normalised():
    sha = hashlib.sha256(b"x").hexdigest()
    assert tenant_key("amaris", f"  {sha.upper()}  ") == f"amaris/{sha}"
