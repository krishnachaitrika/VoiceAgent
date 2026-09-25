"""Tests for VA-B1: the dashboard/settings/documents/analytics auth gate."""
import pytest
from fastapi import HTTPException

import config
from auth import require_dashboard_auth, require_dashboard_admin


async def test_missing_header_is_rejected():
    with pytest.raises(HTTPException) as exc_info:
        await require_dashboard_auth(authorization=None)
    assert exc_info.value.status_code == 401


async def test_wrong_key_is_rejected():
    with pytest.raises(HTTPException):
        await require_dashboard_auth(authorization="Bearer not-the-right-key")


async def test_read_key_grants_read_access():
    await require_dashboard_auth(authorization=f"Bearer {config.DASHBOARD_API_KEY}")


async def test_admin_key_also_grants_read_access():
    await require_dashboard_auth(authorization=f"Bearer {config.DASHBOARD_ADMIN_KEY}")


async def test_read_key_does_not_grant_admin_access():
    with pytest.raises(HTTPException) as exc_info:
        await require_dashboard_admin(authorization=f"Bearer {config.DASHBOARD_API_KEY}")
    assert exc_info.value.status_code == 401


async def test_admin_key_grants_admin_access():
    await require_dashboard_admin(authorization=f"Bearer {config.DASHBOARD_ADMIN_KEY}")


async def test_non_bearer_scheme_is_rejected():
    with pytest.raises(HTTPException):
        await require_dashboard_auth(authorization=config.DASHBOARD_API_KEY)  # no "Bearer " prefix


async def test_unset_configured_key_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_API_KEY", "")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_KEY", "")
    with pytest.raises(HTTPException):
        await require_dashboard_auth(authorization="Bearer anything")
