"""Tests for the terminal state of a cancelled job's progress message.

A cancel must leave the progress message in a cancelled state. Two things can
undo that:

1. A progress update that the worker sent before it saw the cancel, and that
   the consumer sends to Telegram after the cancelled edit.
2. The consumer reads the stored status text of the job before every edit, thus
   a stale text in Redis comes back even when the queued edit is discarded.

The cancelled status text uses a sequence above the 0-100 progress range, thus
Redis keeps it and every later edit renders it.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import fakeredis.aioredis
import pytest

from dumpyarabot import arq_config, arq_jobs
from dumpyarabot.message_queue import MessageQueue


DOWNLOADING = {
    "current_step": " Downloading firmware...",
    "total_steps": 25,
    "current_step_number": 4,
    "percentage": 16.0,
}
CANCELLED = {
    "current_step": "Cancelled",
    "total_steps": 25,
    "current_step_number": 4,
    "percentage": 16.0,
}


@pytest.fixture
async def redis_client():
    """A clean fakeredis instance that decodes to str, like production."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


def test_cancelled_status_outranks_every_progress_percentage():
    assert arq_jobs._status_update_sequence(CANCELLED) > 100.0
    assert arq_jobs._status_update_sequence({"percentage": 100.0}) == 100.0


async def test_in_flight_progress_cannot_replace_cancelled_text(redis_client):
    queue = MessageQueue()
    queue._redis = redis_client

    await queue.store_latest_status_text(
        "job1", "cancelled text", sequence=arq_jobs._status_update_sequence(CANCELLED)
    )
    # An update the worker sent before it saw the cancel arrives afterwards.
    await queue.store_latest_status_text(
        "job1", "16% downloading", sequence=arq_jobs._status_update_sequence(DOWNLOADING)
    )

    assert await queue.get_latest_status_text("job1") == "cancelled text"


async def test_cancelled_notification_edits_the_progress_message(monkeypatch):
    queue = AsyncMock()
    monkeypatch.setattr(arq_jobs, "message_queue", queue)

    job_data = {
        "job_id": "0bf549f22360d904",
        "worker_id": "arq@0bf549f2",
        "initial_message_id": 777,
        "initial_chat_id": -100500,
        "dump_args": {"url": "https://example.com/fw.zip", "use_privdump": False},
        "metadata": {
            "start_time": "2026-01-01T00:00:00+00:00",
            "progress_history": [{"message": " Downloading firmware...", "percentage": 16.0}],
        },
    }

    await arq_jobs._send_cancelled_notification(job_data)

    text = queue.send_status_update.await_args.kwargs["text"]
    assert "Firmware Dump Cancelled" in text
    assert "Cancelled at:  Downloading firmware..." in text
    assert queue.send_status_update.await_args.kwargs["edit_message_id"] == 777
    assert queue.send_status_update.await_args.kwargs["chat_id"] == -100500
    # The stored text must win over any progress text still in flight.
    assert queue.store_latest_status_text.await_args.kwargs["sequence"] > 100.0


async def test_aborted_job_marks_its_message_and_reraises(monkeypatch):
    """ARQ's clean abort cancels the job task. CancelledError is a
    BaseException, thus the `except Exception` handler of the job never saw
    it and the progress message stayed at its last step for all time."""
    queue = AsyncMock()
    monkeypatch.setattr(arq_jobs, "message_queue", queue)
    monkeypatch.setattr(
        arq_config.arq_pool, "is_job_cancel_requested", AsyncMock(return_value=False)
    )
    # Stand in for a cancel that lands inside the dump.
    monkeypatch.setattr(
        arq_jobs, "FirmwareDownloader", Mock(side_effect=asyncio.CancelledError)
    )

    job_data = {
        "job_id": "0bf549f22360d904",
        "initial_message_id": 777,
        "initial_chat_id": -100500,
        "dump_args": {"url": "https://example.com/fw.zip", "use_privdump": False},
    }

    with pytest.raises(asyncio.CancelledError):
        await arq_jobs.process_firmware_dump({"job_id": "0bf549f22360d904"}, job_data)

    assert job_data["metadata"]["status"] == "cancelled"
    assert "Firmware Dump Cancelled" in queue.send_status_update.await_args.kwargs["text"]
