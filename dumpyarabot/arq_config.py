"""ARQ configuration and pool management.

This module provides ARQ configuration and connection pool management
that integrates with the existing Redis configuration.
"""

import json
import os
import signal as _signal
import subprocess
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import arq
from arq import ArqRedis
from arq.constants import (
    health_check_key_suffix,
    in_progress_key_prefix,
    job_key_prefix,
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

    async def register_running_job(self, job_id: str, worker_id: str, pid: int) -> None:
        """Record which worker process currently owns a running job."""
        pool = await self.get_pool()
        payload = json.dumps({"worker_id": worker_id, "pid": pid})
        await pool.set(self._make_running_job_key(job_id), payload, ex=WorkerSettings.job_timeout + 300)

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
        pool = await self.get_pool()
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

        # No live worker — delete the stale in-progress key so ARQ stops
        # treating the job as running and the worker can pick up the next job.
        ip_key = self._make_job_key(in_progress_key_prefix, job_id)
        deleted = 0
        if not owner_alive and not live_child_pids:
            deleted = await pool.delete(ip_key)
            await self.clear_running_job(job_id)
            await self.clear_job_processes(job_id)
        if deleted:
            console.print(f"[yellow]Deleted stale in-progress key for job {job_id}[/yellow]")
            return True

        return acted

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


# Global ARQ pool instance
arq_pool = ARQPool()


async def init_arq():
    """Initialize ARQ pool (call this at startup)."""
    await arq_pool.get_pool()
    console.print("[green]ARQ system initialized[/green]")


async def shutdown_arq():
    """Shutdown ARQ pool (call this at shutdown)."""
    await arq_pool.close()
    console.print("[yellow]ARQ system shutdown[/yellow]")
