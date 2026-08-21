"""Tests for admin-permission checks.

The key behaviour under test: a failure to *reach* Telegram while checking admin
status must surface as a transient verification error, never as a permissions
denial. Previously a proxy timeout was swallowed and reported to the user as
"you don't have permission", which is wrong — the check simply never ran.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import TimedOut

from dumpyarabot.auth import check_admin_permissions
from dumpyarabot.config import settings

CHAT_ID = -1001412293127


@pytest.fixture(autouse=True)
def _allow_chat(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_CHATS", [CHAT_ID])


def _update(chat_id=CHAT_ID, user_id=42):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
    )


def _context(*, status=None, exc=None):
    bot = MagicMock()
    if exc is not None:
        bot.get_chat_member = AsyncMock(side_effect=exc)
    else:
        bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=status))
    return SimpleNamespace(bot=bot)


async def test_admin_is_allowed():
    result = await check_admin_permissions(_update(), _context(status="administrator"))
    assert result.allowed is True
    assert result.verification_failed is False


async def test_creator_is_allowed():
    result = await check_admin_permissions(_update(), _context(status="creator"))
    assert result.allowed is True


async def test_non_admin_is_a_real_denial():
    result = await check_admin_permissions(_update(), _context(status="member"))
    assert result.allowed is False
    assert result.verification_failed is False  # genuine denial, not an error


async def test_wrong_chat_is_denied_without_calling_telegram():
    ctx = _context(status="creator")
    result = await check_admin_permissions(_update(chat_id=-9999), ctx)
    assert result.allowed is False
    assert result.verification_failed is False
    ctx.bot.get_chat_member.assert_not_awaited()


async def test_network_timeout_is_a_verification_failure_not_a_denial():
    result = await check_admin_permissions(
        _update(), _context(exc=TimedOut())
    )
    assert result.allowed is False
    # This is the whole point: a timeout is flagged as a verification failure so
    # the caller can say "try again", not "you don't have permission".
    assert result.verification_failed is True


async def test_unexpected_error_is_also_a_verification_failure():
    result = await check_admin_permissions(
        _update(), _context(exc=RuntimeError("boom"))
    )
    assert result.allowed is False
    assert result.verification_failed is True


async def test_missing_chat_or_user_is_not_a_verification_failure():
    update = SimpleNamespace(effective_chat=None, effective_user=None)
    result = await check_admin_permissions(update, _context(status="creator"))
    assert result.allowed is False
    assert result.verification_failed is False
