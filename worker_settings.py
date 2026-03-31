"""
ARQ Worker settings file.

This is the standard way to configure ARQ workers.
Run with: arq worker_settings.WorkerSettings
"""

from dumpyarabot.arq_config import WorkerSettings

# Export the settings for ARQ CLI
__all__ = ['WorkerSettings']