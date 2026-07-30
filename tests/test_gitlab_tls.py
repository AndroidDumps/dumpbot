"""Tests for the GitLab-specific TLS policy."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dumpyarabot.arq_jobs import _validate_gitlab_access
from dumpyarabot.config import settings
from dumpyarabot.gitlab_manager import GITLAB_BASE_URL, gitlab_http_client


@pytest.mark.parametrize("verify_ssl", [True, False])
def test_gitlab_client_uses_configured_tls_policy(verify_ssl):
    with (
        patch.object(settings, "GITLAB_VERIFY_SSL", verify_ssl),
        patch("dumpyarabot.gitlab_manager.httpx.AsyncClient") as client_class,
    ):
        gitlab_http_client()

    client_class.assert_called_once_with(verify=verify_ssl)


async def test_gitlab_validation_uses_scoped_client():
    response = MagicMock(status_code=200)
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    context_manager = MagicMock()
    context_manager.__aenter__ = AsyncMock(return_value=client)
    context_manager.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "dumpyarabot.arq_jobs.gitlab_http_client",
        return_value=context_manager,
    ) as client_factory:
        await _validate_gitlab_access()

    client_factory.assert_called_once_with()
    client.get.assert_awaited_once_with(
        GITLAB_BASE_URL,
        timeout=10.0,
    )
