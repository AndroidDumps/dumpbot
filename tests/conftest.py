"""Shared pytest fixtures and test-time environment setup."""

import os

import pytest

# dumpyarabot.config.settings is a pydantic Settings instance that requires
# TELEGRAM_BOT_TOKEN and DUMPER_TOKEN at import time. The test suite does not
# need real credentials — set placeholders before any dumpyarabot import that
# transitively loads config. setdefault preserves any real env vars the user
# has set locally.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("DUMPER_TOKEN", "test-token")


@pytest.fixture
async def fake_redis():
    """A clean fakeredis instance for each test."""
    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()
