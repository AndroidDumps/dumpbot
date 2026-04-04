"""ARQ configuration and pool management.

This module provides ARQ configuration and connection pool management
that integrates with the existing Redis configuration.
"""

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import arq
from arq import ArqRedis
from arq.constants import health_check_key_suffix, in_progress_key_prefix
from rich.console import Console

from dumpyarabot.config import settings

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


class WorkerSettings:
    """ARQ Worker Settings configuration."""

    # Redis connection (use same Redis as message queue)
    redis_settings = get_redis_settings()

    # Worker configuration
    queue_name = f"{settings.REDIS_KEY_PREFIX}arq_jobs"
    job_timeout = 7200  # 2 hours max per job
    keep_result = 3600  # Keep job results for 1 hour (will be overridden by result_ttl)
    max_jobs = 1  # Process one job at a time per worker

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

    # Job functions registered lazily to avoid import-time failures
    @classmethod
    def get_functions(cls):
        from dumpyarabot.arq_jobs import process_firmware_dump
        return [process_firmware_dump]


def get_job_result_ttl(status: str) -> int:
    """Get TTL for job result based on status."""
    return WorkerSettings.result_ttl.get(status, WorkerSettings.result_ttl["running"])


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

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel an ARQ job."""
        pool = await self.get_pool()

        try:
            job = arq.jobs.Job(job_id, pool, _queue_name=WorkerSettings.queue_name)
            status = await job.status()
            if status == arq.jobs.JobStatus.not_found:
                return False

            success = await job.abort(timeout=5)

            if success:
                console.print(f"[green]Cancelled ARQ job {job_id}[/green]")
            else:
                console.print(f"[yellow]Could not cancel ARQ job {job_id}[/yellow]")

            return success
        except Exception as e:
            console.print(f"[red]Error cancelling job {job_id}: {e}[/red]")
            return False

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
