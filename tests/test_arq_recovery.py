"""Unit tests for ARQ worker crash recovery."""

import json
import time
from unittest.mock import AsyncMock, mock_open, patch

import arq.jobs
import pytest
from arq.constants import (
    abort_jobs_ss,
    in_progress_key_prefix,
    job_key_prefix,
    result_key_prefix,
    retry_key_prefix,
)

from dumpyarabot import arq_config
from dumpyarabot.config import settings


class TestProcStartTime:
    def test_parses_field_22_from_simple_comm(self):
        # Real /proc/<pid>/stat fields, abbreviated. Field 1=pid, 2=comm,
        # 3=state, ..., 22=starttime. Here starttime=12345.
        line = "1234 (python) S 1 1234 1234 0 -1 4194304 100 0 0 0 1 2 0 0 20 0 1 0 12345 0 0 ..."
        with patch("builtins.open", mock_open(read_data=line)):
            assert arq_config._proc_start_time(1234) == 12345

    def test_parses_field_22_when_comm_contains_spaces_and_parens(self):
        # comm can be anything inside parens — including ") (" — so the parser
        # must split on the LAST ')' in the line.
        line = "1234 (weird ) comm (with) parens) S 1 1234 1234 0 -1 4194304 100 0 0 0 1 2 0 0 20 0 1 0 67890 0 0 ..."
        with patch("builtins.open", mock_open(read_data=line)):
            assert arq_config._proc_start_time(1234) == 67890

    def test_returns_none_on_file_not_found(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            assert arq_config._proc_start_time(99999) is None

    def test_returns_none_on_permission_error(self):
        with patch("builtins.open", side_effect=PermissionError):
            assert arq_config._proc_start_time(1) is None

    def test_returns_none_on_malformed_line(self):
        # No ')' in the line — parser cannot find comm boundary.
        with patch("builtins.open", mock_open(read_data="garbage")):
            assert arq_config._proc_start_time(1234) is None

    def test_returns_none_on_too_few_fields(self):
        # Line has a comm but fewer than 22 fields after.
        with patch("builtins.open", mock_open(read_data="1 (x) S 1 1 1 1")):
            assert arq_config._proc_start_time(1) is None


class TestReadBootId:
    def test_reads_and_strips_boot_id(self):
        with patch("builtins.open", mock_open(read_data="abc-123-def\n")):
            assert arq_config._read_boot_id() == "abc-123-def"

    def test_returns_none_on_file_not_found(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            assert arq_config._read_boot_id() is None

    def test_returns_none_on_permission_error(self):
        with patch("builtins.open", side_effect=PermissionError):
            assert arq_config._read_boot_id() is None


class TestPidOwnerAlive:
    def test_alive_when_boot_pid_and_start_match(self, mocker):
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-A")
        mocker.patch("os.kill")  # raises nothing → PID alive
        mocker.patch("dumpyarabot.arq_config._proc_start_time", return_value=42)
        assert arq_config._pid_owner_alive(pid=1234, start_time=42, boot_id="BOOT-A") is True

    def test_orphan_when_boot_id_differs(self, mocker):
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-B")
        assert arq_config._pid_owner_alive(pid=1234, start_time=42, boot_id="BOOT-A") is False

    def test_orphan_when_stored_boot_id_is_none(self, mocker):
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-A")
        assert arq_config._pid_owner_alive(pid=1234, start_time=42, boot_id=None) is False

    def test_orphan_when_pid_does_not_exist(self, mocker):
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-A")
        mocker.patch("os.kill", side_effect=ProcessLookupError)
        assert arq_config._pid_owner_alive(pid=1234, start_time=42, boot_id="BOOT-A") is False

    def test_orphan_when_start_time_differs(self, mocker):
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-A")
        mocker.patch("os.kill")
        mocker.patch("dumpyarabot.arq_config._proc_start_time", return_value=99)
        assert arq_config._pid_owner_alive(pid=1234, start_time=42, boot_id="BOOT-A") is False

    def test_alive_when_proc_unreadable_but_pid_alive(self, mocker):
        # Fallback path: /proc unreadable, bare PID check says alive — accept.
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-A")
        mocker.patch("os.kill")
        mocker.patch("dumpyarabot.arq_config._proc_start_time", return_value=None)
        assert arq_config._pid_owner_alive(pid=1234, start_time=42, boot_id="BOOT-A") is True

    def test_orphan_when_stored_start_time_is_none_and_proc_returns_value(self, mocker):
        # Pre-upgrade entry: no start_time stored. Cannot verify start; treat as
        # alive only if we cannot read /proc either. Here proc returns 42 → mismatch.
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-A")
        mocker.patch("os.kill")
        mocker.patch("dumpyarabot.arq_config._proc_start_time", return_value=42)
        # stored start_time=None, current=42 → cannot verify → orphan
        assert arq_config._pid_owner_alive(pid=1234, start_time=None, boot_id="BOOT-A") is False


async def _seed_orphan(fake_redis, *, job_id: str, payload: dict, queue_name: str,
                       pid: int, start_time: int, boot_id: str):
    """Seed Redis with an in-progress job that looks like a crashed worker
    left behind: arq:in-progress, arq:job, queue ZSET entry, and a
    running_job blob whose owner is not actually alive."""
    job_blob = arq.jobs.serialize_job(
        function_name="process_firmware_dump",
        args=(payload,),
        kwargs={},
        job_try=1,
        enqueue_time_ms=int(time.time() * 1000),
    )
    await fake_redis.set(f"{job_key_prefix}{job_id}", job_blob)
    await fake_redis.set(f"{in_progress_key_prefix}{job_id}", b"1")
    await fake_redis.zadd(queue_name, {job_id: int(time.time() * 1000)})
    running_key = f"{settings.REDIS_KEY_PREFIX}running_job:{job_id}"
    await fake_redis.set(
        running_key,
        json.dumps({
            "worker_id": "arq@dead",
            "pid": pid,
            "start_time": start_time,
            "boot_id": boot_id,
        }),
    )


class TestRegisterRunningJob:
    async def test_stores_full_owner_blob(self, mocker, fake_redis):
        # Wire arq_pool to use the fake redis directly.
        pool = arq_config.ARQPool()
        mocker.patch.object(pool, "get_pool", AsyncMock(return_value=fake_redis))

        mocker.patch("os.getpid", return_value=4321)
        mocker.patch("dumpyarabot.arq_config._proc_start_time", return_value=555)
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-A")

        await pool.register_running_job("job-1", "arq@worker-x")

        key = f"{settings.REDIS_KEY_PREFIX}running_job:job-1"
        raw = await fake_redis.get(key)
        assert raw is not None
        decoded = json.loads(raw)
        assert decoded == {
            "worker_id": "arq@worker-x",
            "pid": 4321,
            "start_time": 555,
            "boot_id": "BOOT-A",
        }


class TestRecoverCrashedJob:
    async def test_orphan_with_blob_writes_failed_result_and_cleans_keys(self, mocker, fake_redis):
        pool = arq_config.ARQPool()
        mocker.patch.object(pool, "get_pool", AsyncMock(return_value=fake_redis))

        # Pretend the recorded owner is on a different boot.
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-NEW")

        # Telegram side-effect: stub out so the test stays Redis-only.
        sent = AsyncMock()
        mocker.patch("dumpyarabot.arq_config._send_recovery_notification", sent)

        queue_name = arq_config.WorkerSettings.queue_name
        payload = {
            "job_id": "job-1",
            "initial_message_id": 10,
            "initial_chat_id": -1001,
            "dump_args": {"url": "https://example.com/firmware.zip"},
        }
        await _seed_orphan(
            fake_redis,
            job_id="job-1",
            payload=payload,
            queue_name=queue_name,
            pid=999999,
            start_time=42,
            boot_id="BOOT-OLD",
        )

        recovered = await pool._recover_crashed_job("job-1")

        assert recovered is True
        # ARQ-owned keys: deleted/zrem'd.
        assert await fake_redis.exists(f"{retry_key_prefix}job-1") == 0
        assert await fake_redis.exists(f"{in_progress_key_prefix}job-1") == 0
        assert await fake_redis.exists(f"{job_key_prefix}job-1") == 0
        assert await fake_redis.zscore(queue_name, "job-1") is None
        # Custom keys: deleted.
        assert await fake_redis.exists(f"{settings.REDIS_KEY_PREFIX}running_job:job-1") == 0
        # Result key: written, deserializable, success=False.
        result_bytes = await fake_redis.get(f"{result_key_prefix}job-1")
        assert result_bytes is not None
        result = arq.jobs.deserialize_result(result_bytes)
        assert result.success is False
        assert isinstance(result.result, Exception)
        # Telegram notification: invoked exactly once with the recovered payload.
        sent.assert_awaited_once()
        sent_payload = sent.await_args.args[0]
        assert sent_payload["initial_message_id"] == 10
        assert sent_payload["initial_chat_id"] == -1001
        # Recovery populates metadata.error_context so the formatter actually
        # renders the failure reason (the gate at message_formatting.py:347
        # requires error_context to be truthy, and the worker was SIGKILLed
        # before any except-block could set it on its own).
        err_ctx = sent_payload["metadata"]["error_context"]
        assert err_ctx["current_step"] == "Worker killed (SIGKILL/OOM/crash)"
        assert "Worker process killed" in err_ctx["message"]
        assert err_ctx["traceback"] is None
        assert sent_payload["metadata"]["status"] == "failed"

    async def test_alive_owner_skips_recovery(self, mocker, fake_redis):
        pool = arq_config.ARQPool()
        mocker.patch.object(pool, "get_pool", AsyncMock(return_value=fake_redis))
        # Owner is "alive": same boot, PID exists, start_time matches.
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-A")
        mocker.patch("os.kill")  # no exception
        mocker.patch("dumpyarabot.arq_config._proc_start_time", return_value=42)

        sent = AsyncMock()
        mocker.patch("dumpyarabot.arq_config._send_recovery_notification", sent)

        queue_name = arq_config.WorkerSettings.queue_name
        await _seed_orphan(
            fake_redis,
            job_id="job-2",
            payload={"job_id": "job-2", "initial_message_id": 1, "initial_chat_id": -1, "dump_args": {}},
            queue_name=queue_name,
            pid=1234,
            start_time=42,
            boot_id="BOOT-A",
        )

        recovered = await pool._recover_crashed_job("job-2")

        assert recovered is False
        # Nothing should have been removed.
        assert await fake_redis.exists(f"{in_progress_key_prefix}job-2") == 1
        assert await fake_redis.zscore(queue_name, "job-2") is not None
        sent.assert_not_awaited()

    async def test_existing_result_key_short_circuits(self, mocker, fake_redis):
        pool = arq_config.ARQPool()
        mocker.patch.object(pool, "get_pool", AsyncMock(return_value=fake_redis))
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-NEW")
        sent = AsyncMock()
        mocker.patch("dumpyarabot.arq_config._send_recovery_notification", sent)

        # Seed a result key directly — recovery should refuse to clobber it.
        await fake_redis.set(f"{result_key_prefix}job-3", b"previously-written")
        queue_name = arq_config.WorkerSettings.queue_name
        await _seed_orphan(
            fake_redis,
            job_id="job-3",
            payload={"job_id": "job-3", "initial_message_id": 1, "initial_chat_id": -1, "dump_args": {}},
            queue_name=queue_name,
            pid=999999,
            start_time=42,
            boot_id="BOOT-OLD",
        )

        recovered = await pool._recover_crashed_job("job-3")

        assert recovered is False
        assert await fake_redis.get(f"{result_key_prefix}job-3") == b"previously-written"
        sent.assert_not_awaited()

    async def test_missing_arq_job_blob_cleans_but_skips_result_and_notification(self, mocker, fake_redis):
        pool = arq_config.ARQPool()
        mocker.patch.object(pool, "get_pool", AsyncMock(return_value=fake_redis))
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-NEW")
        sent = AsyncMock()
        mocker.patch("dumpyarabot.arq_config._send_recovery_notification", sent)

        queue_name = arq_config.WorkerSettings.queue_name
        # Seed everything EXCEPT arq:job:<id>.
        await fake_redis.set(f"{in_progress_key_prefix}job-4", b"1")
        await fake_redis.zadd(queue_name, {"job-4": int(time.time() * 1000)})
        await fake_redis.set(
            f"{settings.REDIS_KEY_PREFIX}running_job:job-4",
            json.dumps({"worker_id": "arq@dead", "pid": 999999, "start_time": 42, "boot_id": "BOOT-OLD"}),
        )

        recovered = await pool._recover_crashed_job("job-4")

        assert recovered is True
        assert await fake_redis.exists(f"{in_progress_key_prefix}job-4") == 0
        assert await fake_redis.zscore(queue_name, "job-4") is None
        # No result key written — we had no payload to encode.
        assert await fake_redis.exists(f"{result_key_prefix}job-4") == 0
        # No Telegram notification — no job_data.
        sent.assert_not_awaited()

    async def test_missing_running_job_returns_false(self, mocker, fake_redis):
        pool = arq_config.ARQPool()
        mocker.patch.object(pool, "get_pool", AsyncMock(return_value=fake_redis))
        # No seed at all.
        recovered = await pool._recover_crashed_job("job-5")
        assert recovered is False

    async def test_in_progress_only_no_running_job_is_recovered(self, mocker, fake_redis):
        """The on_job_start hook narrows but does not fully close the
        in-progress/running_job gap. If a SIGKILL hits between ARQ setting
        arq:in-progress (worker.py:465) and on_job_start firing (worker.py:584),
        the in-progress key exists with no running_job. Recovery must still
        clean it up."""
        pool = arq_config.ARQPool()
        mocker.patch.object(pool, "get_pool", AsyncMock(return_value=fake_redis))
        mocker.patch("dumpyarabot.arq_config._send_recovery_notification", AsyncMock())

        queue_name = arq_config.WorkerSettings.queue_name
        # Seed in-progress + job blob + queue entry, but NO running_job.
        payload = {"job_id": "job-6", "initial_message_id": 1, "initial_chat_id": -1, "dump_args": {}}
        job_blob = arq.jobs.serialize_job(
            function_name="process_firmware_dump",
            args=(payload,),
            kwargs={},
            job_try=1,
            enqueue_time_ms=int(time.time() * 1000),
        )
        await fake_redis.set(f"{job_key_prefix}job-6", job_blob)
        await fake_redis.set(f"{in_progress_key_prefix}job-6", b"1")
        await fake_redis.zadd(queue_name, {"job-6": int(time.time() * 1000)})

        recovered = await pool._recover_crashed_job("job-6")

        assert recovered is True
        assert await fake_redis.exists(f"{in_progress_key_prefix}job-6") == 0
        assert await fake_redis.zscore(queue_name, "job-6") is None
        # Result key written despite missing running_job — we had a valid job blob.
        assert await fake_redis.exists(f"{result_key_prefix}job-6") == 1


class TestOnStartup:
    async def test_scans_and_recovers_all_orphans(self, mocker, fake_redis):
        pool = arq_config.arq_pool  # module-global instance
        mocker.patch.object(pool, "get_pool", AsyncMock(return_value=fake_redis))
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-NEW")
        mocker.patch("dumpyarabot.arq_config._send_recovery_notification", AsyncMock())

        queue_name = arq_config.WorkerSettings.queue_name
        for job_id in ("orphan-a", "orphan-b", "orphan-c"):
            await _seed_orphan(
                fake_redis,
                job_id=job_id,
                payload={"job_id": job_id, "initial_message_id": 1, "initial_chat_id": -1, "dump_args": {}},
                queue_name=queue_name,
                pid=999999,
                start_time=42,
                boot_id="BOOT-OLD",
            )

        await arq_config.on_startup(ctx={})

        for job_id in ("orphan-a", "orphan-b", "orphan-c"):
            assert await fake_redis.exists(f"{in_progress_key_prefix}{job_id}") == 0
            assert await fake_redis.zscore(queue_name, job_id) is None

    async def test_per_job_exception_does_not_break_others(self, mocker, fake_redis):
        pool = arq_config.arq_pool
        mocker.patch.object(pool, "get_pool", AsyncMock(return_value=fake_redis))
        mocker.patch("dumpyarabot.arq_config._read_boot_id", return_value="BOOT-NEW")
        mocker.patch("dumpyarabot.arq_config._send_recovery_notification", AsyncMock())

        queue_name = arq_config.WorkerSettings.queue_name
        for job_id in ("orphan-x", "orphan-y"):
            await _seed_orphan(
                fake_redis,
                job_id=job_id,
                payload={"job_id": job_id, "initial_message_id": 1, "initial_chat_id": -1, "dump_args": {}},
                queue_name=queue_name,
                pid=999999,
                start_time=42,
                boot_id="BOOT-OLD",
            )

        # Make the first recovery raise; the second should still succeed.
        original = pool._recover_crashed_job
        call_count = {"n": 0}
        async def flaky(jid):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("synthetic")
            return await original(jid)
        mocker.patch.object(pool, "_recover_crashed_job", side_effect=flaky)

        await arq_config.on_startup(ctx={})

        # At least one of the two orphans got cleaned. (Order from SCAN is not
        # deterministic, but the second call always succeeds.)
        cleaned = 0
        for jid in ("orphan-x", "orphan-y"):
            if await fake_redis.exists(f"{in_progress_key_prefix}{jid}") == 0:
                cleaned += 1
        assert cleaned >= 1

    async def test_top_level_failure_does_not_raise(self, mocker, fake_redis):
        pool = arq_config.arq_pool
        mocker.patch.object(pool, "get_pool", AsyncMock(side_effect=RuntimeError("redis down")))
        # Must not raise: a hot-restart loop is worse than skipping recovery.
        await arq_config.on_startup(ctx={})

    async def test_in_progress_only_orphan_picked_up_via_in_progress_scan(self, mocker, fake_redis):
        """on_startup must also scan arq:in-progress:* and recover ids with
        no matching running_job. This covers the residual window where a kill
        hits between ARQ setting in-progress (worker.py:465) and on_job_start
        firing (worker.py:584)."""
        pool = arq_config.arq_pool
        mocker.patch.object(pool, "get_pool", AsyncMock(return_value=fake_redis))
        mocker.patch("dumpyarabot.arq_config._send_recovery_notification", AsyncMock())

        queue_name = arq_config.WorkerSettings.queue_name
        payload = {"job_id": "ghost-job", "initial_message_id": 1, "initial_chat_id": -1, "dump_args": {}}
        job_blob = arq.jobs.serialize_job(
            function_name="process_firmware_dump",
            args=(payload,),
            kwargs={},
            job_try=1,
            enqueue_time_ms=int(time.time() * 1000),
        )
        await fake_redis.set(f"{job_key_prefix}ghost-job", job_blob)
        await fake_redis.set(f"{in_progress_key_prefix}ghost-job", b"1")
        await fake_redis.zadd(queue_name, {"ghost-job": int(time.time() * 1000)})
        # Crucially: NO <prefix>running_job:ghost-job key.

        await arq_config.on_startup(ctx={})

        assert await fake_redis.exists(f"{in_progress_key_prefix}ghost-job") == 0
        assert await fake_redis.zscore(queue_name, "ghost-job") is None
        assert await fake_redis.exists(f"{result_key_prefix}ghost-job") == 1

    async def test_in_progress_for_foreign_queue_is_ignored(self, mocker, fake_redis):
        """Cross-queue safety: arq:in-progress:* is global, not queue-scoped.
        If we find an in-progress key for a job not in OUR queue ZSET, leave
        it alone — it belongs to a different worker/queue on shared Redis."""
        pool = arq_config.arq_pool
        mocker.patch.object(pool, "get_pool", AsyncMock(return_value=fake_redis))
        sent = AsyncMock()
        mocker.patch("dumpyarabot.arq_config._send_recovery_notification", sent)

        # Seed an in-progress key WITHOUT adding to our queue ZSET.
        await fake_redis.set(f"{in_progress_key_prefix}foreign-job", b"1")
        # And a foreign job blob (irrelevant — our scan should bail before reading it).
        await fake_redis.set(f"{job_key_prefix}foreign-job", b"foreign-bytes")

        await arq_config.on_startup(ctx={})

        # Untouched.
        assert await fake_redis.exists(f"{in_progress_key_prefix}foreign-job") == 1
        assert await fake_redis.exists(f"{job_key_prefix}foreign-job") == 1
        assert await fake_redis.exists(f"{result_key_prefix}foreign-job") == 0
        sent.assert_not_awaited()
