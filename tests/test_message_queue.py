import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError

from dumpyarabot import lua_scripts
from dumpyarabot.message_queue import (
    MessagePriority,
    MessageQueue,
    MessageType,
    QueuedMessage,
    sanitize_telegram_error,
)


CHAT_ID = -1001
EDIT_MESSAGE_ID = 42


def _edit_message(sequence=None) -> QueuedMessage:
    return QueuedMessage(
        type=MessageType.STATUS_UPDATE,
        priority=MessagePriority.NORMAL,
        chat_id=CHAT_ID,
        text="Step 20/25",
        edit_message_id=EDIT_MESSAGE_ID,
        edit_sequence=sequence,
        context={"job_id": "job-1"},
    )


def test_sanitize_telegram_error_truncates_html_and_preserves_status():
    error = NetworkError(
        "Bad Gateway (502). Parsing the server response "
        "b'<!DOCTYPE html>\\n<html><body>" + ("proxy failure " * 100) + "</body></html>' failed"
    )

    sanitized = sanitize_telegram_error(error)

    assert sanitized.startswith("NetworkError: HTTP 502; non-JSON/HTML response:")
    assert "\n" not in sanitized
    assert len(sanitized) < 280
    assert "proxy failure" in sanitized


def test_sanitize_telegram_error_labels_plain_non_json_response():
    error = NetworkError(
        "Bad Gateway (502). Parsing the server response b'upstream unavailable' failed"
    )

    sanitized = sanitize_telegram_error(error)

    assert sanitized == (
        "NetworkError: HTTP 502; non-JSON response: b'upstream unavailable'"
    )


@pytest.mark.asyncio
async def test_publish_atomically_stamps_new_edit_sequence():
    queue = MessageQueue()
    redis = AsyncMock()
    redis.eval.return_value = 7
    queue._redis = redis
    message = _edit_message()

    await queue.publish(message)

    assert message.edit_sequence == 7
    stamp_call = redis.eval.await_args_list[0]
    assert stamp_call.args[0] == lua_scripts.STAMP_EDIT_SEQUENCE
    # The script needs both the counter key and the watermark key, so that it
    # can restart the counter at the watermark after Redis loses the counter.
    assert stamp_call.args[1] == 2
    assert "msg_edit_seq:" in stamp_call.args[2]
    assert "msg_edit_applied:" in stamp_call.args[3]
    redis.lpush.assert_awaited_once()
    assert '"edit_sequence":7' in redis.lpush.await_args.args[1]


@pytest.mark.asyncio
async def test_process_drops_edit_older_than_last_applied_sequence():
    queue = MessageQueue()
    redis = AsyncMock()
    redis.eval.return_value = 1
    queue._redis = redis
    queue._bot = AsyncMock()
    message = _edit_message(sequence=20)

    assert await queue._process_message(message) is True

    stale_call = redis.eval.await_args_list[0]
    assert stale_call.args[0] == lua_scripts.IS_STALE_EDIT
    queue._bot.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_successful_edit_cas_marks_sequence_applied():
    queue = MessageQueue()
    redis = AsyncMock()
    redis.eval.side_effect = [0, 1]
    queue._redis = redis
    queue._bot = AsyncMock()
    queue.get_latest_status_text = AsyncMock(return_value=None)
    message = _edit_message(sequence=21)

    assert await queue._process_message(message) is True

    queue._bot.edit_message_text.assert_awaited_once()
    assert redis.eval.await_args_list[1].args[0] == lua_scripts.MARK_EDIT_APPLIED


@pytest.mark.asyncio
async def test_delayed_retry_compare_and_enqueue_is_one_redis_operation():
    queue = MessageQueue()
    redis = AsyncMock()
    redis.eval.return_value = 0
    queue._redis = redis
    message = _edit_message(sequence=20)
    message.scheduled_for = datetime.now(timezone.utc) + timedelta(seconds=30)

    await queue._requeue_message(message)

    requeue_call = redis.eval.await_args_list[0]
    assert requeue_call.args[0] == lua_scripts.REQUEUE_EDIT_IF_CURRENT
    assert requeue_call.args[1] == 2
    assert requeue_call.args[5] == "zset"
    redis.zadd.assert_not_called()
    redis.lpush.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_operation_has_hard_wall_clock_deadline():
    queue = MessageQueue()

    with pytest.raises(NetworkError, match="hard deadline"):
        await queue._call_telegram(asyncio.sleep(1), timeout=0.001)


# The tests below run the real Lua scripts against an in-process Redis. The
# mocked tests above show which script the code calls. These tests show that
# the scripts give the correct result.


def _fake_redis():
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_later_edit_supersedes_earlier_failed_edit():
    """Step 21 succeeds, thus the bot must discard the retry of step 20."""
    queue = MessageQueue()
    queue._redis = _fake_redis()

    step20 = _edit_message()
    step21 = _edit_message()
    await queue.publish(step20)
    await queue.publish(step21)
    assert step20.edit_sequence < step21.edit_sequence

    # Step 20 fails and waits for a retry. Step 21 then succeeds.
    await queue._mark_edit_applied(step21)

    # The bot must discard step 20 and must not put it in the queue again.
    assert await queue._is_stale_edit(step20) is True
    queue_key = queue._make_queue_key(step20.priority)
    before = await queue._redis.llen(queue_key)
    await queue._requeue_message(step20)
    assert await queue._redis.llen(queue_key) == before

    # A later edit must continue to operate.
    step22 = _edit_message()
    await queue.publish(step22)
    assert await queue._is_stale_edit(step22) is False


@pytest.mark.asyncio
async def test_counter_restarts_at_watermark_when_redis_loses_the_counter():
    """A lost counter key must not stop all subsequent edits."""
    queue = MessageQueue()
    queue._redis = _fake_redis()

    for _ in range(50):
        message = _edit_message()
        await queue.publish(message)
        await queue._mark_edit_applied(message)

    counter_key = queue._make_edit_sequence_key(CHAT_ID, EDIT_MESSAGE_ID)
    await queue._redis.delete(counter_key)

    # Without the watermark, the counter would start again at 1. The bot would
    # then discard this edit and every edit after it.
    message = _edit_message()
    await queue.publish(message)
    assert message.edit_sequence == 51
    assert await queue._is_stale_edit(message) is False


@pytest.mark.asyncio
async def test_edit_without_sequence_is_dropped_when_a_newer_edit_applied():
    """A message from a version before this one must not show an earlier step."""
    queue = MessageQueue()
    queue._redis = _fake_redis()
    queue._bot = AsyncMock()

    # A new edit applies and moves the watermark.
    current = _edit_message()
    await queue.publish(current)
    await queue._mark_edit_applied(current)

    # This message has no order number, thus the bot must discard it.
    legacy = _edit_message()
    legacy.edit_sequence = None
    legacy.context = {}

    assert await queue._is_stale_edit(legacy) is True
    assert await queue._process_message(legacy) is True
    queue._bot.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_edit_without_sequence_is_sent_when_no_edit_applied():
    """With no watermark, a message with no order number must go to Telegram."""
    queue = MessageQueue()
    queue._redis = _fake_redis()
    queue._bot = AsyncMock()

    legacy = _edit_message()
    legacy.edit_sequence = None
    legacy.context = {}

    assert await queue._is_stale_edit(legacy) is False
    assert await queue._process_message(legacy) is True
    queue._bot.edit_message_text.assert_awaited_once()


def test_sanitize_telegram_error_ignores_a_status_code_in_the_body():
    """A number in brackets in the body is not the status code."""
    error = NetworkError("Parsing the server response b'see error (404) below' failed")

    sanitized = sanitize_telegram_error(error)

    assert "HTTP 404" not in sanitized
