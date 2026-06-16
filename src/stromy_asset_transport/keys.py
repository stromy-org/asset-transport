"""Tenant-scoping helper for content-addressed handles.

The store keys blobs by bare ``sha256(bytes)`` so identical assets dedupe across
every tenant (a deliberate property — see :mod:`stromy_asset_transport.store`).
Tenant *isolation*, where a caller must not resolve or propose a handle that was
not staged for one of its own slugs, is enforced by the **caller** (e.g. the
asset-broker staging ledger), not by partitioning the store. This helper builds
the ACL/ledger key a caller uses for that bookkeeping — it never changes the
store's dedup key.
"""

from __future__ import annotations


def tenant_key(tenant: str | None, sha256: str) -> str:
    """Return the caller-side ACL key for ``sha256`` scoped to ``tenant``.

    ``tenant is None`` (or empty) returns the bare ``sha256`` — the unscoped form
    used when no tenant isolation applies. A non-empty ``tenant`` returns
    ``"<tenant>/<sha256>"`` for the caller's staging ledger / access-control map.
    This value is **never** used as the store's blob key (which stays bare
    ``sha256`` for cross-tenant dedup).
    """
    sha = sha256.strip().lower()
    if not tenant:
        return sha
    return f"{tenant.strip()}/{sha}"
