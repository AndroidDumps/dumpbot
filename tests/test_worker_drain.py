"""Draining a worker for a code reload (SIGUSR1 / `systemctl reload`)."""

import asyncio
import os
import signal
from types import SimpleNamespace

from run_arq_worker import DRAIN_EXIT_CODE, ARQWorkerManager


def _manager_with_tasks(*tasks):
    """Build a manager whose worker is running the given tasks.

    Args:
        *tasks: asyncio tasks standing in for running jobs.

    Returns:
        An ARQWorkerManager with a minimal worker attached.
    """
    manager = ARQWorkerManager("test_worker")
    manager.worker = SimpleNamespace(
        allow_pick_jobs=True, tasks={f"job{i}": t for i, t in enumerate(tasks)}
    )
    return manager


async def test_drain_stops_new_jobs_and_waits_for_running_one():
    """A drain stops picking jobs, waits for the running job, then exits 75."""
    release = asyncio.Event()
    job = asyncio.create_task(release.wait())
    manager = _manager_with_tasks(job)

    manager.request_drain()
    await asyncio.sleep(1.5)

    assert manager.worker.allow_pick_jobs is False
    assert not manager.shutdown_event.is_set()  # the dump is still running

    release.set()
    await asyncio.wait_for(manager.shutdown_event.wait(), timeout=5)
    assert job.done()
    assert manager.exit_code == DRAIN_EXIT_CODE


async def test_sigusr1_triggers_drain():
    """SIGUSR1 drains an idle worker straight away."""
    manager = _manager_with_tasks()
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1)}
    try:
        manager._setup_signal_handlers()
        os.kill(os.getpid(), signal.SIGUSR1)
        await asyncio.wait_for(manager.shutdown_event.wait(), timeout=5)
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)

    assert manager.worker.allow_pick_jobs is False
    assert manager.exit_code == DRAIN_EXIT_CODE
