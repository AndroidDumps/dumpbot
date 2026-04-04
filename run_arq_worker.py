#!/usr/bin/env python3
"""
ARQ Worker Script

This script runs ARQ workers to process firmware dump jobs.
Uses ARQ's built-in worker management for improved performance and reliability.

Usage:
    python run_arq_worker.py [worker_name]

Examples:
    python run_arq_worker.py
    python run_arq_worker.py worker_01
"""

import asyncio
import sys
import signal
from typing import Optional

import arq
from rich.console import Console

from dumpyarabot.arq_config import WorkerSettings, shutdown_arq

console = Console()


class ARQWorkerManager:
    """Manages ARQ worker lifecycle with graceful shutdown."""

    def __init__(self, worker_name: Optional[str] = None):
        self.worker_name = worker_name or "arq_worker"
        self.worker: Optional[arq.Worker] = None
        self.shutdown_event = asyncio.Event()

    async def start_worker(self):
        """Start the ARQ worker."""
        console.print(f"[green]Starting ARQ worker: {self.worker_name}[/green]")

        try:
            # Create worker with our settings using the correct API
            self.worker = arq.Worker(
                functions=WorkerSettings.get_functions(),
                redis_settings=WorkerSettings.redis_settings,
                max_jobs=WorkerSettings.max_jobs,
                job_timeout=WorkerSettings.job_timeout,
                keep_result=WorkerSettings.keep_result,
                max_tries=WorkerSettings.max_tries,
                health_check_interval=WorkerSettings.health_check_interval,
                allow_abort_jobs=WorkerSettings.allow_abort_jobs,
                queue_name=WorkerSettings.queue_name
            )

            # Set up signal handlers for graceful shutdown
            self._setup_signal_handlers()

            console.print(f"[blue]Worker {self.worker_name} started and waiting for jobs...[/blue]")

            worker_task = asyncio.create_task(self.worker.async_run())
            shutdown_task = asyncio.create_task(self.shutdown_event.wait())

            done, pending = await asyncio.wait(
                {worker_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if worker_task in done:
                exc = worker_task.exception()
                if exc is not None:
                    raise exc

            if shutdown_task in done and worker_task not in done:
                console.print(f"[yellow]Shutdown event received, stopping worker {self.worker_name}...[/yellow]")
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass

            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            console.print(f"[red]Error starting worker {self.worker_name}: {e}[/red]")
            raise

    def _setup_signal_handlers(self):
        """Set up signal handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            console.print(f"[yellow]Received signal {signum}, initiating graceful shutdown...[/yellow]")
            self.shutdown_event.set()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    async def shutdown(self):
        """Gracefully shutdown the worker."""
        console.print(f"[yellow]Shutting down worker {self.worker_name}...[/yellow]")

        if self.worker:
            await self.worker.close()

        await shutdown_arq()
        console.print(f"[green]Worker {self.worker_name} shutdown complete[/green]")


async def main():
    """Main entry point for ARQ worker."""
    worker_name = sys.argv[1] if len(sys.argv) > 1 else None

    manager = ARQWorkerManager(worker_name)

    try:
        await manager.start_worker()
    except KeyboardInterrupt:
        console.print("[yellow]Keyboard interrupt received[/yellow]")
    except Exception as e:
        console.print(f"[red]Worker crashed: {e}[/red]")
        sys.exit(1)
    finally:
        await manager.shutdown()


if __name__ == "__main__":
    # Show startup banner
    console.print("\n[bold blue] ARQ Firmware Dump Worker[/bold blue]")
    console.print("[dim]Processing firmware dumps with ARQ queue system[/dim]\n")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Worker stopped by user[/yellow]")
    except Exception as e:
        console.print(f"\n[red]Fatal error: {e}[/red]")
        sys.exit(1)
