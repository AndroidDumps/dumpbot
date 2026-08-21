"""Tests for the dump flow when the Telegram proxy is slow.

The bot reaches Telegram through a proxy. A slow proxy makes a send time out.
These tests check two behaviours:

1. The first status message send retries after a network timeout. A bad request
   does not retry. It goes up to the caller.
2. The worker makes a status message when the job has none. The worker keeps the
   message location in Redis. A retry or a recovery uses the same message. This
   stops duplicate messages.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from telegram.error import BadRequest, Forbidden, TimedOut

from dumpyarabot import handlers
from dumpyarabot.message_queue import MessageQueue


@pytest.fixture
async def redis_client():
    """A clean fakeredis instance that decodes to str, like production."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


def _make_queue(redis_client):
    queue = MessageQueue()
    queue._redis = redis_client
    queue._bot = AsyncMock()
    return queue


# --------------------------------------------------------------------------
# _send_initial_message_with_retry
# --------------------------------------------------------------------------


async def test_initial_send_success_returns_message(monkeypatch):
    message = SimpleNamespace(message_id=42)
    send = AsyncMock(return_value=message)
    monkeypatch.setattr(handlers.message_queue, "send_immediate_message", send)

    result = await handlers._send_initial_message_with_retry(
        chat_id=-100, text="hi", reply_to_message_id=None
    )

    assert result is message
    assert send.await_count == 1


async def test_initial_send_retries_then_gives_up_on_timeout(monkeypatch):
    monkeypatch.setattr(handlers, "INITIAL_MESSAGE_RETRY_DELAY", 0)
    send = AsyncMock(side_effect=TimedOut())
    monkeypatch.setattr(handlers.message_queue, "send_immediate_message", send)

    result = await handlers._send_initial_message_with_retry(
        chat_id=-100, text="hi", reply_to_message_id=None
    )

    # Every try times out. The function returns None. The caller then queues
    # the job without the message.
    assert result is None
    assert send.await_count == handlers.INITIAL_MESSAGE_SEND_ATTEMPTS


async def test_initial_send_does_not_retry_bad_request(monkeypatch):
    # BadRequest is a NetworkError subclass but it is not transient. It must go
    # up to the caller after one try. It must not become a silent "queue anyway".
    monkeypatch.setattr(handlers, "INITIAL_MESSAGE_RETRY_DELAY", 0)
    send = AsyncMock(side_effect=BadRequest("bad markdown"))
    monkeypatch.setattr(handlers.message_queue, "send_immediate_message", send)

    with pytest.raises(BadRequest):
        await handlers._send_initial_message_with_retry(
            chat_id=-100, text="hi", reply_to_message_id=None
        )
    assert send.await_count == 1


# --------------------------------------------------------------------------
# store / get status message reference
# --------------------------------------------------------------------------


async def test_status_message_ref_round_trip(redis_client):
    queue = _make_queue(redis_client)
    await queue.store_status_message_ref("job1", -100500, 777)
    assert await queue.get_status_message_ref("job1") == (-100500, 777)


async def test_status_message_ref_missing_returns_none(redis_client):
    queue = _make_queue(redis_client)
    assert await queue.get_status_message_ref("nope") is None


# --------------------------------------------------------------------------
# verify_telegram_context
# --------------------------------------------------------------------------


async def test_verify_creates_message_when_missing_and_stores_ref(redis_client):
    queue = _make_queue(redis_client)
    queue._bot.send_message.return_value = SimpleNamespace(message_id=777)

    job_data = {
        "job_id": "job1",
        "initial_message_id": None,
        "initial_chat_id": None,
        "metadata": {"telegram_context": {"chat_id": -100500}},
        "_queued_text": "Queued...",
        "dump_args": {"initial_message_id": None},
    }

    await queue.verify_telegram_context(job_data)

    # It made a new message and did not edit.
    queue._bot.send_message.assert_awaited_once()
    queue._bot.edit_message_text.assert_not_awaited()
    # It backfilled the job data.
    assert job_data["initial_message_id"] == 777
    assert job_data["initial_chat_id"] == -100500
    assert job_data["metadata"]["telegram_context"]["message_id"] == 777
    # It stored the location for a later retry.
    assert await queue.get_status_message_ref("job1") == (-100500, 777)


async def test_verify_reuses_stored_message_instead_of_creating(redis_client):
    queue = _make_queue(redis_client)
    await queue.store_status_message_ref("job1", -100500, 555)

    job_data = {
        "job_id": "job1",
        "initial_message_id": None,
        "initial_chat_id": None,
        "metadata": {"telegram_context": {"chat_id": -100500}},
        "_queued_text": "Queued...",
        "dump_args": {"initial_message_id": None},
    }

    await queue.verify_telegram_context(job_data)

    # It found the stored message. It edited that message. It did not make a
    # second message.
    queue._bot.send_message.assert_not_awaited()
    queue._bot.edit_message_text.assert_awaited_once()
    assert job_data["initial_message_id"] == 555


async def test_verify_raises_when_bot_is_blocked_on_create(redis_client):
    queue = _make_queue(redis_client)
    queue._bot.send_message.side_effect = Forbidden("bot was blocked by the user")

    job_data = {
        "job_id": "job1",
        "initial_message_id": None,
        "initial_chat_id": None,
        "metadata": {"telegram_context": {"chat_id": -100500}},
        "_queued_text": "Queued...",
        "dump_args": {"initial_message_id": None},
    }

    # A blocked bot is a permanent error. The job must abort before heavy work.
    with pytest.raises(RuntimeError):
        await queue.verify_telegram_context(job_data)
