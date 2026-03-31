"""ARQ configuration and pool management.

This module provides ARQ configuration and connection pool management
that integrates with the existing Redis configuration.
"""

import asyncio
from typing import Optional
from urllib.parse import urlparse

import arq
from arq import ArqRedis
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


def get_job_result_ttl(status: str) -> int:
    """Get TTL for job result based on status."""
    return WorkerSettings.result_ttl.get(status, WorkerSettings.result_ttl["running"])


class WorkerSettings:
    """ARQ Worker Settings configuration."""

    # Redis connection (use same Redis as message queue)
    redis_settings = get_redis_settings()

    # Job functions to register (import directly to avoid string resolution issues)
    from dumpyarabot.arq_jobs import process_firmware_dump
    functions = [
        process_firmware_dump
    ]

    # Worker configuration
    queue_name = f"{settings.REDIS_KEY_PREFIX}arq_jobs"
    job_timeout = 7200  # 2 hours max per job
    keep_result = 3600  # Keep job results for 1 hour (will be overridden by result_ttl)
    max_jobs = 1  # Process one job at a time per worker

    # Retry configuration
    max_tries = 3

    # Health check
    health_check_interval = 30

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

        job_id = await pool.enqueue_job(
            function_name,
            *args,
            _job_id=job_id,
            _queue_name=WorkerSettings.queue_name,
            **kwargs
        )

        console.print(f"[green]Enqueued ARQ job {job_id} ({function_name}) to queue {WorkerSettings.queue_name}[/green]")
        return job_id

    async def get_job_status(self, job_id: str) -> Optional[dict]:
        """Get job status from ARQ."""
        pool = await self.get_pool()

        try:
            job_result = await arq.jobs.JobResult.create(pool, job_id)

            if job_result is None:
                return None

            return {
                "job_id": job_id,
                "status": job_result.status.name if job_result.status else "unknown",
                "result": job_result.result,
                "enqueue_time": job_result.enqueue_time.isoformat() if job_result.enqueue_time else None,
                "start_time": job_result.start_time.isoformat() if job_result.start_time else None,
                "finish_time": job_result.finish_time.isoformat() if job_result.finish_time else None,
                "success": job_result.success
            }
        except Exception as e:
            console.print(f"[yellow]Could not get job status for {job_id}: {e}[/yellow]")
            return None

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel an ARQ job."""
        pool = await self.get_pool()

        try:
            # In newer ARQ versions, we try to abort the job
            success = await pool.abort_job(job_id)

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
            queue_length = await pool.llen(WorkerSettings.queue_name)

            # Get health check info (if available)
            health_check_key = f"{WorkerSettings.queue_name}:health-check"
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