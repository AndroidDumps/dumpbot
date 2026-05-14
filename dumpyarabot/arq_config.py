"""ARQ configuration and pool management.

This module provides ARQ configuration and connection pool management
that integrates with the existing Redis configuration.
"""

import json
import os
import shutil
import signal as _signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import arq
import arq.jobs
from arq import ArqRedis
from arq.constants import (
    abort_jobs_ss,
    health_check_key_suffix,
    in_progress_key_prefix,
    job_key_prefix,
    result_key_prefix,
    retry_key_prefix,
)
from rich.console import Console

from dumpyarabot.config import settings
from dumpyarabot.schemas import JobCancelResult

console = Console()


def get_redis_settings():
    """Parse Redis URL and return connection settings."""
    parsed = urlparse(settings.REDIS_URL)

    return arq.connections.RedisSettings(
        host=parsed.hostname or 'localhost',
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip('/')) if parsed.path and parsed.path != '/' else 0,
        password=parsed.password,
        username=parsed.username
    )


def get_job_result_ttl(status: str) -> int:
    """Get TTL for job result based on status."""
    return WorkerSettings.result_ttl.get(status, WorkerSettings.result_ttl["running"])


class WorkerSettings:
    """ARQ Worker Settings configuration."""

    # Redis connection (use same Redis as message queue)
    redis_settings = get_redis_settings()

    # Job functions registered lazily to avoid import-time failures
    @classmethod
    def get_functions(cls):
        from dumpyarabot.arq_jobs import process_firmware_dump
        return [process_firmware_dump]

    # Worker configuration
    queue_name = f"{settings.REDIS_KEY_PREFIX}arq_jobs"
    job_timeout = 7200  # 2 hours max per job
    keep_result = 3600  # Keep job results for 1 hour (will be overridden by result_ttl)
    max_jobs = settings.ARQ_MAX_JOBS

    # Retry configuration
    max_tries = 3

    # Health check
    health_check_interval = 30
    allow_abort_jobs = True

    # Logging
    log_results = True

    # Result TTL configuration (seconds)
    result_ttl = {
        "completed": 60 * 24 * 3600,  # 60 days
        "failed": 15 * 24 * 3600,     # 15 days
        "running": 7 * 24 * 3600,     # 7 days
    }


class ARQPool:
    """ARQ Redis connection pool manager."""

    def __init__(self):
        self._pool: Optional[ArqRedis] = None
        self._closed = False

    async def get_pool(self) -> ArqRedis:
        """Get or create ARQ Redis pool."""
        if self._pool is None or self._closed:
            self._pool = await arq.connections.create_pool(WorkerSettings.redis_settings)
            self._closed = False
            console.print("[green]ARQ Redis pool created[/green]")
        return self._pool

    async def enqueue_job(
        self,
        function_name: str,
        *args,
        job_id: Optional[str] = None,
        **kwargs
    ) -> str:
        """Enqueue a job using ARQ."""
        pool = await self.get_pool()

        job = await pool.enqueue_job(
            function_name,
            *args,
            _job_id=job_id,
            _queue_name=WorkerSettings.queue_name,
            **kwargs
        )

        if job is None:
            raise RuntimeError(f"Job {job_id!r} already exists in queue or results")

        console.print(f"[green]Enqueued ARQ job {job.job_id} ({function_name}) to queue {WorkerSettings.queue_name}[/green]")
        return job.job_id

    async def get_job_status(self, job_id: str) -> Optional[dict]:
        """Get job status from ARQ."""
        pool = await self.get_pool()

        try:
            job = arq.jobs.Job(job_id, pool, _queue_name=WorkerSettings.queue_name)
            status = await job.status()
            if status == arq.jobs.JobStatus.not_found:
                return None

            job_info = await job.info()
            result_info = await job.result_info()

            payload: Optional[Dict[str, Any]] = None
            source_info = job_info or result_info
            if source_info and source_info.args:
                first_arg = source_info.args[0]
                if isinstance(first_arg, dict):
                    payload = first_arg

            metadata = {}
            if isinstance(payload, dict) and isinstance(payload.get("metadata"), dict):
                metadata = payload["metadata"]
            if result_info and isinstance(result_info.result, dict) and isinstance(result_info.result.get("metadata"), dict):
                metadata = result_info.result["metadata"]

            status_value = status.value
            if result_info:
                if metadata.get("status") in {"completed", "failed", "cancelled"}:
                    status_value = metadata["status"]
                elif result_info.success:
                    status_value = "completed"
                else:
                    status_value = "failed"

            return {
                "job_id": job_id,
                "status": status_value,
                "result": result_info.result if result_info else None,
                "enqueue_time": source_info.enqueue_time.isoformat() if source_info and source_info.enqueue_time else None,
                "start_time": result_info.start_time.isoformat() if result_info else None,
                "finish_time": result_info.finish_time.isoformat() if result_info else None,
                "success": result_info.success if result_info else None,
                "job_data": payload,
            }
        except Exception as e:
            console.print(f"[yellow]Could not get job status for {job_id}: {e}[/yellow]")
            return None

    def _make_job_key(self, prefix: Any, job_id: str) -> Any:
        """Build a Redis key from an ARQ key prefix."""
        if isinstance(prefix, bytes):
            return prefix + job_id.encode()
        return f"{prefix}{job_id}"

    def _make_running_job_key(self, job_id: str) -> str:
        """Build the Redis key for per-job worker ownership metadata."""
        return f"{settings.REDIS_KEY_PREFIX}running_job:{job_id}"

    def _make_job_processes_key(self, job_id: str) -> str:
        """Build the Redis key for subprocess PIDs associated with a job."""
        return f"{settings.REDIS_KEY_PREFIX}job_processes:{job_id}"

    def _make_cancel_requested_key(self, job_id: str) -> str:
        """Build the Redis key for a cooperative cancellation request."""
        return f"{settings.REDIS_KEY_PREFIX}cancel_requested:{job_id}"

    async def register_running_job(self, job_id: str, worker_id: str) -> None:
        """Record which worker process currently owns a running job.

        Stores PID, /proc/<pid>/stat starttime, and current boot_id so
        recovery can later detect PID reuse and cross-reboot stale entries.
        """
        pool = await self.get_pool()
        pid = os.getpid()
        payload = json.dumps({
            "worker_id": worker_id,
            "pid": pid,
            "start_time": _proc_start_time(pid),
            "boot_id": _read_boot_id(),
        })
        await pool.set(
            self._make_running_job_key(job_id),
            payload,
            ex=WorkerSettings.job_timeout + 300,
        )

    async def get_running_job_owner(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the worker ownership metadata for a running job."""
        pool = await self.get_pool()
        raw_value = await pool.get(self._make_running_job_key(job_id))
        if not raw_value:
            return None

        try:
            value = raw_value if isinstance(raw_value, str) else raw_value.decode()
            owner = json.loads(value)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None

        if not isinstance(owner, dict):
            return None

        return owner

    async def clear_running_job(self, job_id: str) -> None:
        """Remove the worker ownership metadata for a job."""
        pool = await self.get_pool()
        await pool.delete(self._make_running_job_key(job_id))

    async def register_job_process(self, job_id: str, pid: int) -> None:
        """Track a subprocess PID associated with a running job."""
        pool = await self.get_pool()
        key = self._make_job_processes_key(job_id)
        await pool.sadd(key, str(pid))
        await pool.expire(key, WorkerSettings.job_timeout + 300)

    async def unregister_job_process(self, job_id: str, pid: int) -> None:
        """Remove a subprocess PID association for a job."""
        pool = await self.get_pool()
        await pool.srem(self._make_job_processes_key(job_id), str(pid))

    async def get_job_processes(self, job_id: str) -> List[int]:
        """Get tracked subprocess PIDs for a job."""
        pool = await self.get_pool()
        raw_pids = await pool.smembers(self._make_job_processes_key(job_id))
        pids: List[int] = []
        for raw_pid in raw_pids:
            try:
                value = raw_pid if isinstance(raw_pid, str) else raw_pid.decode()
                pids.append(int(value))
            except (AttributeError, TypeError, ValueError):
                continue
        return pids

    async def clear_job_processes(self, job_id: str) -> None:
        """Remove tracked subprocess PIDs for a job."""
        pool = await self.get_pool()
        await pool.delete(self._make_job_processes_key(job_id))

    async def request_job_cancel(self, job_id: str) -> None:
        """Set a cooperative cancellation flag for a job."""
        pool = await self.get_pool()
        await pool.set(self._make_cancel_requested_key(job_id), "1", ex=WorkerSettings.job_timeout + 300)

    async def is_job_cancel_requested(self, job_id: str) -> bool:
        """Check whether a cooperative cancellation was requested for a job."""
        pool = await self.get_pool()
        return bool(await pool.exists(self._make_cancel_requested_key(job_id)))

    async def clear_job_cancel_request(self, job_id: str) -> None:
        """Clear the cooperative cancellation flag for a job."""
        pool = await self.get_pool()
        await pool.delete(self._make_cancel_requested_key(job_id))

    async def cancel_job(self, job_id: str) -> JobCancelResult:
        """Cancel an ARQ job without corrupting worker state."""
        pool = await self.get_pool()

        try:
            job = arq.jobs.Job(job_id, pool, _queue_name=WorkerSettings.queue_name)
            status = await job.status()
            if status == arq.jobs.JobStatus.not_found:
                return JobCancelResult.NOT_FOUND

            # Try soft abort (30s covers PeriodicTimerUpdate's sleep interval)
            if await job.abort(timeout=30):
                console.print(f"[green]Cleanly cancelled ARQ job {job_id}[/green]")
                return JobCancelResult.CANCELLED

            # Re-check status: job may have completed naturally during the 30s wait
            if await job.status() == arq.jobs.JobStatus.not_found:
                console.print(f"[blue]Job {job_id} completed during abort wait[/blue]")
                return JobCancelResult.NOT_FOUND

            # Still running — escalate to force-kill
            console.print(f"[yellow]Soft abort timed out for {job_id}, escalating to force-kill[/yellow]")
            await self.request_job_cancel(job_id)
            if await self.force_cancel_job(job_id):
                return JobCancelResult.FORCE_KILLED

            owner = await self.get_running_job_owner(job_id)
            if owner and owner.get("pid") is not None:
                try:
                    os.kill(int(owner["pid"]), 0)
                    console.print(
                        f"[yellow]Worker for job {job_id} is still alive after abort timeout; "
                        f"waiting for cooperative cancellation[/yellow]"
                    )
                    return JobCancelResult.CANCELLING
                except ProcessLookupError:
                    pass
                except (TypeError, ValueError):
                    pass

            return JobCancelResult.TIMED_OUT
        except Exception as e:
            console.print(f"[red]Error cancelling job {job_id}: {e}[/red]")
            return JobCancelResult.FAILED

    async def force_cancel_job(self, job_id: str) -> bool:
        """Hard-kill a stuck in-progress job.

        Uses per-job worker ownership metadata so multiple workers can coexist
        without a global PID collision. If the worker is already gone, falls
        back to clearing stale Redis state for that job only.

        Returns True if any corrective action was taken.
        """
        owner = await self.get_running_job_owner(job_id)
        child_pids = await self.get_job_processes(job_id)
        acted = False
        live_child_pids: List[int] = []

        if child_pids:
            for pid in child_pids:
                try:
                    self._terminate_process_tree(pid)
                    console.print(f"[yellow]Terminated subprocess tree rooted at PID {pid} for job {job_id}[/yellow]")
                    acted = True
                except ProcessLookupError:
                    console.print(f"[yellow]Subprocess PID {pid} for job {job_id} no longer running[/yellow]")
                except OSError as e:
                    console.print(f"[yellow]Could not signal subprocess PID {pid} for job {job_id}: {e}[/yellow]")
                    live_child_pids.append(pid)

        owner_alive = False
        if owner and owner.get("pid") is not None:
            try:
                pid = int(owner["pid"])
                os.kill(pid, 0)
                owner_alive = True
                if not acted:
                    console.print(
                        f"[yellow]No tracked child processes remained for job {job_id}; "
                        f"waiting for cooperative cancellation on worker {owner.get('worker_id', 'unknown')}[/yellow]"
                    )
            except ProcessLookupError:
                console.print(
                    f"[yellow]Worker PID {owner.get('pid')!r} for job {job_id} no longer running; "
                    f"clearing stale Redis state[/yellow]"
                )
                owner = None
            except (TypeError, ValueError):
                console.print(
                    f"[yellow]Invalid worker PID in running-job metadata for {job_id}: "
                    f"{owner.get('pid')!r}[/yellow]"
                )
                owner = None

        # No live worker — delegate to the shared recovery helper, which
        # does the full atomic cleanup (queue/abort ZREMs, retry/in-progress/
        # job DELs, synthetic failed result) plus custom-key cleanup.
        if not owner_alive and not live_child_pids:
            recovered = await self._recover_crashed_job(job_id)
            if recovered:
                console.print(f"[yellow]Force-cancel recovered orphan job {job_id}[/yellow]")
                return True

        return acted

    async def _recover_crashed_job(self, job_id: str) -> bool:
        """Mark an orphaned job as failed and clean ARQ state.

        Returns True if recovery was performed, False if the job is either
        still owned by a live worker or already terminal.

        Mirrors arq.worker.Worker.finish_failed_job's MULTI/EXEC cleanup
        (worker.py:713-727) and uses arq.jobs.serialize_result for the
        synthetic result payload so arq.jobs.Job.result_info() can read it.
        """
        pool = await self.get_pool()

        # 1. Idempotency guard.
        if await pool.exists(f"{result_key_prefix}{job_id}"):
            return False

        # 2. Owner liveness check.
        running_key = self._make_running_job_key(job_id)
        raw = await pool.get(running_key)

        if raw is None:
            # No running_job metadata. This can happen two ways:
            #  (a) The SCAN found this id only via arq:in-progress:<id> — meaning
            #      the worker died inside the run_job pre-hook window. There is
            #      no recoverable owner; recovery should still clean ARQ state.
            #  (b) Another path already cleaned the metadata but the in-progress
            #      key lingered (shouldn't happen, but be defensive).
            # Both require arq:in-progress:<id> to actually exist; otherwise
            # the id is fully cleaned and we're done.
            if not await pool.exists(f"{in_progress_key_prefix}{job_id}"):
                return False
            # Cross-queue safety: arq:in-progress:* is GLOBAL (not queue-scoped).
            # If we only see in-progress and no running_job, the job might belong
            # to a foreign ARQ queue sharing this Redis. Verify it's in OUR queue
            # before clobbering it. If the job is mid-flight in another worker on
            # a different queue, our queue ZSET will not contain its id.
            if await pool.zscore(WorkerSettings.queue_name, job_id) is None:
                return False
            owner = {}
        else:
            try:
                value = raw if isinstance(raw, str) else raw.decode()
                owner = json.loads(value)
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                owner = {}
            if not isinstance(owner, dict):
                owner = {}

            raw_pid = owner.get("pid")
            try:
                pid_int = int(raw_pid) if raw_pid is not None else 0
            except (TypeError, ValueError):
                pid_int = 0
            if pid_int <= 0:
                # Malformed/missing owner PID; cannot verify liveness — treat as orphan.
                pid_alive = False
            else:
                pid_alive = _pid_owner_alive(
                    pid=pid_int,
                    start_time=owner.get("start_time"),
                    boot_id=owner.get("boot_id"),
                )
            if pid_alive:
                return False

        # 3. Load original job spec (may be missing).
        job_blob = await pool.get(f"{job_key_prefix}{job_id}")
        function_name: Optional[str] = None
        args: tuple = ()
        kwargs: dict = {}
        job_try: int = 1
        enqueue_time_ms: int = int(time.time() * 1000)
        job_data: Optional[Dict[str, Any]] = None
        result_bytes: Optional[bytes] = None

        if job_blob is not None:
            try:
                function_name, args, kwargs, job_try, enqueue_time_ms = arq.jobs.deserialize_job_raw(job_blob)
            except Exception as exc:
                console.print(f"[yellow]Recovery: could not deserialize arq:job for {job_id}: {exc}[/yellow]")
                function_name = None

            # 4. Build synthetic failed result (only if we have a function name).
            if function_name is not None:
                try:
                    result_bytes = arq.jobs.serialize_result(
                        function=function_name,
                        args=args,
                        kwargs=kwargs,
                        job_try=job_try,
                        enqueue_time_ms=enqueue_time_ms,
                        success=False,
                        result=Exception("Worker process killed before job completed"),
                        start_ms=enqueue_time_ms,
                        finished_ms=int(time.time() * 1000),
                        ref=f"{job_id}:{function_name}",
                        queue_name=WorkerSettings.queue_name,
                        job_id=job_id,
                    )
                except Exception as exc:
                    console.print(f"[yellow]Recovery: serialize_result failed for {job_id}: {exc}[/yellow]")
                    result_bytes = None

            # Build job_data for Telegram notification (only if args[0] is a dict).
            if args and isinstance(args[0], dict):
                job_data = args[0]

        # 5. Atomic Redis cleanup (mirrors Worker.finish_failed_job).
        keep_forever = getattr(WorkerSettings, "keep_result_forever", False)
        keep_result_s = getattr(WorkerSettings, "keep_result", 0) or 0

        async with pool.pipeline(transaction=True) as tr:
            tr.delete(
                f"{retry_key_prefix}{job_id}",
                f"{in_progress_key_prefix}{job_id}",
                f"{job_key_prefix}{job_id}",
            )
            tr.zrem(abort_jobs_ss, job_id)
            tr.zrem(WorkerSettings.queue_name, job_id)
            if result_bytes is not None and (keep_forever or keep_result_s > 0):
                if keep_forever:
                    # Redis rejects PX=0; omit the expiration for keep-forever.
                    tr.set(f"{result_key_prefix}{job_id}", result_bytes)
                else:
                    tr.set(
                        f"{result_key_prefix}{job_id}",
                        result_bytes,
                        px=int(keep_result_s * 1000),
                    )
            await tr.execute()

        # 6. Clean custom keys (outside the transaction).
        await self.clear_running_job(job_id)
        await self.clear_job_processes(job_id)
        await self.clear_job_cancel_request(job_id)

        # 7. Telegram notification (best-effort).
        if job_data is not None:
            reason = "Worker process killed before job completed. Please re-submit."
            # Populate metadata.error_context so the formatter renders the
            # failure reason. Without this, format_comprehensive_progress_message
            # and _build_failure_log_text both silently drop the error text
            # because their gate requires error_context to be truthy.
            metadata = job_data.setdefault("metadata", {})
            progress_history = metadata.get("progress_history") or []
            last_step = (
                progress_history[-1].get("message", "Unknown step")
                if progress_history
                else "Worker killed before any step"
            )
            metadata["error_context"] = {
                "message": reason,
                "current_step": "Worker killed (SIGKILL/OOM/crash)",
                "last_successful_step": last_step,
                "failure_time": datetime.now(timezone.utc).isoformat(),
                "traceback": None,
            }
            metadata.setdefault("status", "failed")
            metadata.setdefault(
                "end_time", datetime.now(timezone.utc).isoformat()
            )
            await _send_recovery_notification(job_data, reason)

        console.print(f"[yellow]Recovered crashed job {job_id}[/yellow]")
        return True

    def _terminate_process_tree(self, pid: int) -> None:
        """Terminate a subprocess tree rooted at the given PID."""
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return

        os.killpg(os.getpgid(pid), _signal.SIGTERM)

    async def clear_queued_jobs(self) -> list:
        """Atomically remove queued jobs and clean their backing Redis keys."""
        pool = await self.get_pool()
        raw_job_ids = await pool.eval(
            """
            local job_ids = redis.call('ZRANGE', KEYS[1], 0, -1)
            if #job_ids > 0 then
                redis.call('DEL', KEYS[1])
            end
            return job_ids
            """,
            1,
            WorkerSettings.queue_name,
        )
        job_ids = [
            jid.decode() if isinstance(jid, bytes) else jid
            for jid in raw_job_ids
        ]
        if job_ids:
            cleanup_keys: List[Any] = []
            for job_id in job_ids:
                cleanup_keys.extend([
                    self._make_job_key(job_key_prefix, job_id),
                    self._make_job_key(retry_key_prefix, job_id),
                ])
            await pool.delete(*cleanup_keys)
            console.print(f"[yellow]Cleared {len(job_ids)} queued job(s)[/yellow]")
        return job_ids

    async def get_queue_stats(self) -> dict:
        """Get ARQ queue statistics."""
        pool = await self.get_pool()

        try:
            # Get queue length
            queue_length = await pool.zcard(WorkerSettings.queue_name)

            # Get health check info (if available)
            health_check_key = f"{WorkerSettings.queue_name}{health_check_key_suffix}"
            health_checks = await pool.lrange(health_check_key, 0, -1)

            return {
                "queue_name": WorkerSettings.queue_name,
                "queue_length": queue_length,
                "active_health_checks": len(health_checks) if health_checks else 0,
                "pool_status": "connected" if self._pool and not self._closed else "disconnected"
            }
        except Exception as e:
            console.print(f"[red]Error getting queue stats: {e}[/red]")
            return {
                "queue_name": WorkerSettings.queue_name,
                "queue_length": 0,
                "active_health_checks": 0,
                "pool_status": "error",
                "error": str(e)
            }

    async def get_active_job_ids(self) -> List[str]:
        """Get queued and in-progress ARQ job ids."""
        pool = await self.get_pool()

        queued_job_ids = [
            job_id.decode() if isinstance(job_id, bytes) else job_id
            for job_id in await pool.zrange(WorkerSettings.queue_name, 0, -1)
        ]
        in_progress_job_ids = [
            (key.decode() if isinstance(key, bytes) else key)[len(in_progress_key_prefix):]
            for key in await pool.keys(f"{in_progress_key_prefix}*")
        ]

        return list(dict.fromkeys(queued_job_ids + in_progress_job_ids))

    async def get_recent_job_results(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent completed ARQ job results for this queue."""
        pool = await self.get_pool()
        results = await pool.all_job_results()

        filtered_results = [
            result for result in results
            if getattr(result, "queue_name", None) == WorkerSettings.queue_name
        ]
        filtered_results.sort(key=lambda result: result.finish_time, reverse=True)

        recent: List[Dict[str, Any]] = []
        for result in filtered_results[:limit]:
            payload = None
            if result.args:
                first_arg = result.args[0]
                if isinstance(first_arg, dict):
                    payload = first_arg

            recent.append({
                "job_id": result.job_id,
                "status": "completed" if result.success else "failed",
                "result": result.result,
                "enqueue_time": result.enqueue_time.isoformat() if result.enqueue_time else None,
                "start_time": result.start_time.isoformat() if result.start_time else None,
                "finish_time": result.finish_time.isoformat() if result.finish_time else None,
                "success": result.success,
                "job_data": payload,
            })

        return recent

    async def close(self):
        """Close the ARQ pool."""
        if self._pool and not self._closed:
            await self._pool.close()
            self._closed = True
            console.print("[yellow]ARQ Redis pool closed[/yellow]")


def _proc_start_time(pid: int) -> Optional[int]:
    """Read field 22 (starttime, in clock ticks since boot) from /proc/<pid>/stat.

    The comm field (#2) can contain spaces and parentheses, so we split on the
    last ')' to find the boundary. After that boundary, the tail starts with
    state (field 3), so field N is at tail-index N-3 and field 22 is index 19.

    Returns None on any read or parse error.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            line = f.read()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None

    rparen = line.rfind(")")
    if rparen < 0:
        return None
    tail = line[rparen + 1 :].split()
    if len(tail) < 20:
        return None
    try:
        return int(tail[19])
    except (TypeError, ValueError):
        return None


def _read_boot_id() -> Optional[str]:
    """Return the current boot identifier (UUID) from /proc/sys/kernel/random/boot_id.

    Changes on every reboot. Returns None on any read error.
    """
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _pid_owner_alive(pid: int, start_time: Optional[int], boot_id: Optional[str]) -> bool:
    """Decide whether the worker process that registered a running_job is still alive.

    Rules:
      - boot_id missing or != current → orphan (host rebooted, or pre-upgrade entry).
      - os.kill(pid, 0) raises ProcessLookupError → orphan.
      - /proc/<pid>/stat field 22 differs from stored start_time → orphan (PID reuse).
      - /proc/<pid>/stat unreadable → fall back to bare PID-liveness (small residual risk).
      - stored start_time is None but /proc gives a value → cannot verify, treat as orphan.
    """
    current_boot = _read_boot_id()
    if boot_id is None or current_boot is None or boot_id != current_boot:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # PermissionError etc. — process exists but we can't signal it.
        return True

    current_start = _proc_start_time(pid)
    if current_start is None:
        # /proc unreadable: trust the bare PID-liveness signal.
        return True
    if start_time is None:
        # Pre-upgrade entry with no recorded start_time; can't verify.
        return False
    return current_start == start_time


async def _send_recovery_notification(job_data: Dict[str, Any], reason: str) -> None:
    """Send a crash-recovery Telegram notification.

    Thin wrapper around dumpyarabot.arq_jobs._send_failure_notification so
    that this module can be imported without circular imports — the function
    is resolved at call time, not import time.
    """
    from dumpyarabot.arq_jobs import _send_failure_notification

    try:
        await _send_failure_notification(job_data, reason)
    except Exception as exc:
        console.print(f"[red]Recovery: failed to send Telegram notification: {exc}[/red]")


# Global ARQ pool instance
arq_pool = ARQPool()


async def on_job_start(ctx: Dict[str, Any]) -> None:
    """ARQ hook: register ownership before the user function runs.

    Narrows (but does not fully close) the race window between ARQ setting
    arq:in-progress:<id> (worker.py:~465, inside start_jobs) and our owner
    metadata being recorded. There is still a small window inside run_job
    where ARQ does job-fetch, retry-increment, and deserialization before
    invoking on_job_start (worker.py:584). The recovery path in on_startup
    handles that residual window by also scanning arq:in-progress:* — any
    in-progress key without a matching running_job is treated as an orphan.
    """
    job_id = ctx.get("job_id")
    if not job_id:
        return
    worker_id = f"arq@{str(job_id)[:8]}"
    await arq_pool.register_running_job(job_id, worker_id)
    await arq_pool.clear_job_cancel_request(job_id)


async def after_job_end(ctx: Dict[str, Any]) -> None:
    """ARQ hook: clean ownership AFTER the result has been recorded.

    We use `after_job_end` (worker.py:679) rather than `on_job_end`
    (worker.py:664) because ARQ runs them in this order:
        on_job_end → finish_job (writes result, DELs arq:in-progress) → after_job_end
    Clearing running_job in `on_job_end` would leave a gap where a SIGKILL
    between `on_job_end` and `finish_job` strands arq:in-progress with no
    matching running_job, which the startup-recovery SCAN cannot see.
    `after_job_end` runs after ARQ has already deleted in-progress, so the
    two states are torn down in the safe order.
    """
    job_id = ctx.get("job_id")
    if not job_id:
        return
    await arq_pool.clear_running_job(job_id)
    await arq_pool.clear_job_processes(job_id)
    await arq_pool.clear_job_cancel_request(job_id)


async def _sweep_stale_work_dirs() -> None:
    """Remove `dump_<job>_<rand>/` dirs left behind by SIGKILL'd workers.

    Multi-worker safe: skips any dir whose job still has a running_job:<id>
    Redis key (i.e. owned by *some* live worker), and skips anything younger
    than job_timeout + 1h (covers the enqueue→pick→register race where a
    brand-new dir has no Redis key for a few seconds).
    """
    base_str = settings.WORK_DIR_BASE
    if not base_str:
        return
    base = Path(base_str)
    if not base.is_dir():
        return

    pool = await arq_pool.get_pool()
    min_age = WorkerSettings.job_timeout + 3600  # 2h + 1h grace
    now = time.time()
    removed = 0
    freed_bytes = 0

    for entry in base.iterdir():
        if not entry.is_dir() or not entry.name.startswith("dump_"):
            continue

        # Parse `dump_<job_id>_<rand>` → job_id. job_id is hex (no '_').
        try:
            job_part = entry.name[len("dump_"):]
            job_id = job_part.rsplit("_", 1)[0]
        except Exception:
            continue
        if not job_id:
            continue

        running_key = f"{settings.REDIS_KEY_PREFIX}running_job:{job_id}"
        if await pool.exists(running_key):
            continue

        try:
            age = now - entry.stat().st_mtime
        except FileNotFoundError:
            continue
        if age < min_age:
            continue

        try:
            size = sum(p.stat().st_size for p in entry.rglob("*") if p.is_file())
        except Exception:
            size = 0
        try:
            shutil.rmtree(entry)
            removed += 1
            freed_bytes += size
            console.print(f"[yellow]Swept stale work dir: {entry} ({size // (1024**3)}G)[/yellow]")
        except Exception as exc:
            console.print(f"[red]Failed to remove {entry}: {exc}[/red]")

    if removed:
        console.print(
            f"[yellow]Swept {removed} stale work dir(s), freed ~{freed_bytes // (1024**3)}G[/yellow]"
        )


async def on_startup(ctx: Dict[str, Any]) -> None:
    """ARQ hook: scan for orphaned in-progress jobs and mark them failed.

    Wrapped in a top-level try/except so a transient Redis error or other
    enumeration failure logs and returns instead of crashing the worker.
    Per-job recovery has its own try/except too.
    """
    try:
        pool = await arq_pool.get_pool()
        running_prefix = f"{settings.REDIS_KEY_PREFIX}running_job:"

        # Collect job ids from both <prefix>running_job:* and arq:in-progress:*.
        # The latter catches the residual window where ARQ sets in-progress
        # before run_job invokes on_job_start (worker.py:465 → :584); a kill
        # in that window leaves an in-progress key with no running_job.
        seen: set = set()

        async def _scan(prefix: str, strip_len: int) -> None:
            pattern = f"{prefix}*"
            cursor = 0
            while True:
                cursor, keys = await pool.scan(cursor=cursor, match=pattern, count=100)
                for key in keys:
                    key_str = key.decode() if isinstance(key, bytes) else key
                    job_id = key_str[strip_len:]
                    if job_id:
                        seen.add(job_id)
                if cursor == 0:
                    break

        await _scan(running_prefix, len(running_prefix))
        await _scan(in_progress_key_prefix, len(in_progress_key_prefix))

        recovered = 0
        for job_id in seen:
            try:
                if await arq_pool._recover_crashed_job(job_id):
                    recovered += 1
            except Exception as exc:
                console.print(
                    f"[red]Recovery for job {job_id} raised: {exc}[/red]"
                )

        if recovered:
            console.print(
                f"[yellow]Recovered {recovered} crashed job(s) from previous worker run[/yellow]"
            )
    except Exception as exc:
        console.print(f"[red]on_startup recovery failed: {exc}[/red]")

    try:
        await _sweep_stale_work_dirs()
    except Exception as exc:
        console.print(f"[red]on_startup work-dir sweep failed: {exc}[/red]")


async def init_arq():
    """Initialize ARQ pool (call this at startup)."""
    await arq_pool.get_pool()
    console.print("[green]ARQ system initialized[/green]")


async def shutdown_arq():
    """Shutdown ARQ pool (call this at shutdown)."""
    await arq_pool.close()
    console.print("[yellow]ARQ system shutdown[/yellow]")
