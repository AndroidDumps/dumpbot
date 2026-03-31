"""ARQ job functions for firmware dump processing.

This module contains ARQ job functions that replace the custom worker system
while preserving all Telegram messaging features and cross-chat functionality.
"""

import asyncio
import re
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from rich.console import Console

from dumpyarabot.config import settings
from dumpyarabot.firmware_downloader import FirmwareDownloader
from dumpyarabot.firmware_extractor import FirmwareExtractor
from dumpyarabot.gitlab_manager import GitLabManager
from dumpyarabot.schemas import DumpJob
from dumpyarabot.message_queue import message_queue
from dumpyarabot.property_extractor import PropertyExtractor
from dumpyarabot.message_formatting import format_comprehensive_progress_message

console = Console()

# Patterns to sanitize from tracebacks to prevent credential exposure
_SENSITIVE_PATTERNS = [
    re.compile(r'(Bearer\s+)\S+', re.IGNORECASE),
    re.compile(r'(Authorization:\s*)\S+', re.IGNORECASE),
    re.compile(r'(token[=:]\s*)\S+', re.IGNORECASE),
    re.compile(r'(password[=:]\s*)\S+', re.IGNORECASE),
]


def _sanitize_traceback(tb_str: str) -> str:
    """Remove sensitive tokens and credentials from traceback strings."""
    for pattern in _SENSITIVE_PATTERNS:
        tb_str = pattern.sub(r'\1[REDACTED]', tb_str)
    return tb_str


class PeriodicTimerUpdate:
    """Context manager for periodic elapsed time updates during long operations."""

    def __init__(self, job_data: Dict[str, Any], message: str, progress: Dict[str, Any], interval: int = 30):
        self.job_data = job_data
        self.message = message
        self.progress = progress
        self.interval = interval
        self.task = None
        self.running = False

    async def __aenter__(self):
        self.running = True
        self.task = asyncio.create_task(self._periodic_update())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.running = False
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _periodic_update(self):
        """Send periodic updates with refreshed elapsed time."""
        try:
            while self.running:
                await asyncio.sleep(self.interval)
                if self.running:  # Check again after sleep
                    await _send_status_update(self.job_data, self.message, self.progress, self.job_data.get("metadata"))
        except asyncio.CancelledError:
            pass


async def _send_status_update(
    job_data: Dict[str, Any],
    message: str,
    progress: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None  # NEW parameter
) -> None:
    """Send a status update message using the existing message queue - PRESERVING ALL TELEGRAM FEATURES."""

    # Format the comprehensive progress message with metadata support
    formatted_message = await format_comprehensive_progress_message(job_data, message, progress, metadata)

    # PRESERVE: Check for required message context (from original logic)
    initial_message_id = job_data.get("initial_message_id")
    initial_chat_id = job_data.get("initial_chat_id")

    if not initial_message_id or not initial_chat_id:
        console.print(f"[red]ERROR: Job {job_data['job_id']} missing initial message reference! Cannot send updates.[/red]")
        return

    chat_id = initial_chat_id

    # PRESERVE: Cross-chat logic for moderated system (exact original logic)
    dump_args_initial_message_id = job_data["dump_args"].get("initial_message_id")

    primary_allowed_chat = settings.ALLOWED_CHATS[0] if settings.ALLOWED_CHATS else None

    if dump_args_initial_message_id and primary_allowed_chat is not None and initial_chat_id != primary_allowed_chat:
        # Cross-chat update for moderated system - edit with cross-chat reply
        await message_queue.send_cross_chat_edit(
            chat_id=primary_allowed_chat,
            text=formatted_message,
            edit_message_id=initial_message_id,
            reply_to_message_id=dump_args_initial_message_id,
            reply_to_chat_id=initial_chat_id,
            context={
                "job_id": job_data["job_id"],
                "worker_id": "arq_worker",
                "progress": progress
            }
        )
    else:
        # Same-chat update - edit the initial message
        await message_queue.send_status_update(
            chat_id=chat_id,
            text=formatted_message,
            edit_message_id=initial_message_id,
            parse_mode=settings.DEFAULT_PARSE_MODE,
            context={
                "job_id": job_data["job_id"],
                "worker_id": "arq_worker",
                "progress": progress
            }
        )


async def _send_failure_notification(job_data: Dict[str, Any], error_details: str) -> None:
    """Send a failure notification using existing message queue - PRESERVING ALL TELEGRAM FEATURES."""

    try:
        # Create progress data to show failure state (exact original logic)
        failure_progress = {
            "current_step": "Failed",
            "total_steps": 10,
            "current_step_number": 0,
            "percentage": 0.0,
            "error_message": error_details
        }

        # Format the failure message using the standard progress format
        progress = job_data.get("progress") or {}
        current_step = progress.get("current_step", "Unknown step")
        formatted_message = await format_comprehensive_progress_message(
            job_data,
            f" Failed at: {current_step}",
            failure_progress
        )

        # PRESERVE: Check for required message context
        initial_message_id = job_data.get("initial_message_id")
        initial_chat_id = job_data.get("initial_chat_id")

        if not initial_message_id or not initial_chat_id:
            console.print(f"[red]ERROR: Job {job_data.get('job_id', 'unknown')} missing initial message reference! Cannot send failure update.[/red]")
            console.print(f"[red]Job data keys: {list(job_data.keys())}[/red]")
            return

        chat_id = initial_chat_id

        # PRESERVE: Cross-chat logic for moderated system (exact original logic)
        dump_args_initial_message_id = job_data.get("dump_args", {}).get("initial_message_id")

        primary_allowed_chat = settings.ALLOWED_CHATS[0] if settings.ALLOWED_CHATS else None

        if dump_args_initial_message_id and primary_allowed_chat is not None and initial_chat_id != primary_allowed_chat:
            # Cross-chat failure update for moderated system - edit with cross-chat reply
            await message_queue.send_cross_chat_edit(
                chat_id=primary_allowed_chat,
                text=formatted_message,
                edit_message_id=initial_message_id,
                reply_to_message_id=dump_args_initial_message_id,
                reply_to_chat_id=initial_chat_id,
                context={"job_id": job_data.get("job_id", "unknown"), "type": "failure"}
            )
        else:
            # Same-chat failure update - edit the initial message
            await message_queue.send_status_update(
                chat_id=chat_id,
                text=formatted_message,
                edit_message_id=initial_message_id,
                parse_mode=settings.DEFAULT_PARSE_MODE,
                context={"job_id": job_data.get("job_id", "unknown"), "type": "failure"}
            )

        console.print(f"[green]Sent failure notification for job {job_data.get('job_id', 'unknown')}[/green]")

    except Exception as e:
        console.print(f"[red]Failed to send failure notification: {e}[/red]")
        console.print_exception()


async def _validate_gitlab_access() -> None:
    """Validate GitLab server access - from original worker logic."""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://dumps.tadiphone.dev", timeout=10.0)
            if response.status_code >= 400:
                raise Exception(f"GitLab server returned {response.status_code}")
            console.print("[green]GitLab server access validated[/green]")
    except Exception as e:
        raise Exception(f"Cannot access GitLab server: {e}")


async def _check_cancellation(ctx) -> bool:
    """Check if the current ARQ job has been aborted."""
    try:
        if asyncio.current_task().cancelled():
            return True
        # Check ARQ's abort flag if available
        if hasattr(ctx, 'abort'):
            return ctx.abort
    except Exception:
        pass
    return False


async def update_progress_with_metadata(
    job_data: Dict[str, Any],
    step: str,
    percentage: float,
    extra_info: Optional[Dict[str, Any]] = None
) -> None:
    """Helper function for progress updates with metadata tracking."""
    # Check for cancellation at each progress update
    ctx = job_data.get("_arq_ctx")
    if ctx and await _check_cancellation(ctx):
        raise asyncio.CancelledError("Job cancelled by user")

    metadata = job_data["metadata"]

    progress_update = {
        "message": step,
        "percentage": percentage,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    if extra_info:
        progress_update.update(extra_info)

    metadata["progress_history"].append(progress_update)

    progress_data = {
        "current_step": step,
        "percentage": percentage,
        "current_step_number": len(metadata["progress_history"]),
        "total_steps": 25
    }

    await _send_status_update(job_data, step, progress_data, metadata)


async def process_firmware_dump(ctx, job_data: Dict[str, Any]) -> Dict[str, Any]:
    """ARQ job with integrated metadata tracking."""
    job_id = job_data["job_id"]
    console.print(f"[blue]ARQ processing job {job_id}[/blue]")

    # Initialize metadata
    job_data["metadata"] = job_data.get("metadata", {})
    job_data["metadata"].update({
        "start_time": datetime.now(timezone.utc).isoformat(),
        "progress_history": [],
        "status": "running"
    })

    # Add ARQ job context to job_data for tracking
    job_data["arq_job_id"] = getattr(ctx, 'job_id', None)
    job_data["_arq_ctx"] = ctx  # Store ctx for cancellation checks

    try:
        # Create temporary work directory
        with tempfile.TemporaryDirectory(prefix=f"dump_{job_id}_") as temp_dir:
            work_dir = Path(temp_dir)
            console.print(f"[blue]Working directory: {work_dir}[/blue]")

            try:
                # Initialize components (exact same as original)
                downloader = FirmwareDownloader(str(work_dir))
                extractor = FirmwareExtractor(str(work_dir))
                prop_extractor = PropertyExtractor(str(work_dir))
                gitlab_manager = GitLabManager(str(work_dir))

                # Step 1: Environment setup and URL validation (4%)
                await update_progress_with_metadata(job_data, " Validating URL and setting up environment...", 4.0)

                # Step 2: GitLab access validation (8%)
                await update_progress_with_metadata(job_data, " Validating GitLab access...", 8.0)
                await _validate_gitlab_access()
                is_whitelisted = await gitlab_manager.check_whitelist(str(job_data["dump_args"]["url"]))

                # Step 3: URL optimization and mirror selection (12%)
                await update_progress_with_metadata(job_data, " Optimizing download URL and selecting mirrors...", 12.0)

                # Step 4: Starting download (16%)
                await update_progress_with_metadata(job_data, " Downloading firmware...", 16.0)

                # Create DumpJob object for components that need it
                dump_job = DumpJob.model_validate(job_data)

                # Use periodic timer for download operation
                download_progress = {
                    "current_step": "Download",
                    "total_steps": 25,
                    "current_step_number": 4,
                    "percentage": 16.0,
                }
                async with PeriodicTimerUpdate(job_data, " Downloading firmware...", download_progress):
                    firmware_path, firmware_name = await downloader.download_firmware(dump_job)

                # Step 5: Download completed (20%)
                await update_progress_with_metadata(job_data, " Firmware download completed", 20.0)

                # Step 6: Starting firmware extraction (24%)
                await update_progress_with_metadata(job_data, " Extracting firmware partitions...", 24.0)

                # Use periodic timer for extraction operation
                async with PeriodicTimerUpdate(job_data, " Extracting firmware partitions...", {"current_step": "Extract", "total_steps": 25, "current_step_number": 6, "percentage": 24.0}):
                    await extractor.extract_firmware(dump_job, firmware_path)

                # Step 7: Python/Alternative dumper completed (28%)
                await update_progress_with_metadata(job_data, " Firmware extraction completed", 28.0)

                # Step 8: Partition extraction completed (32%)
                await update_progress_with_metadata(job_data, " Partition extraction completed", 32.0)

                # Step 9: Extracting device properties (36%)
                await update_progress_with_metadata(job_data, " Extracting device properties...", 36.0)
                device_props = await prop_extractor.extract_properties()
                job_data["metadata"]["device_info"] = device_props
                await update_progress_with_metadata(job_data, " Device analysis completed", 48.0)

                # Step 13: Checking/creating GitLab subgroup (52%)
                await update_progress_with_metadata(job_data, " Checking GitLab subgroup...", 52.0)

                # Step 14: Checking/creating GitLab project (56%)
                await update_progress_with_metadata(job_data, " Checking GitLab project...", 56.0)

                # Step 15: Setting up git repository (60%)
                await update_progress_with_metadata(job_data, " Creating GitLab repository...", 60.0)

                # Get DUMPER_TOKEN from environment or settings
                dumper_token = getattr(settings, 'DUMPER_TOKEN', None)
                if not dumper_token:
                    raise Exception("DUMPER_TOKEN not configured")

                # Use periodic timer for GitLab operation (longest operation)
                gitlab_progress = {
                    "current_step": "GitLab",
                    "total_steps": 25,
                    "current_step_number": 15,
                    "percentage": 60.0,
                }
                async with PeriodicTimerUpdate(job_data, " Creating GitLab repository...", gitlab_progress):
                    repo_url, repo_path = await gitlab_manager.create_and_push_repository(device_props, dumper_token)

                # Step 16: Preparing channel notification (64%)
                await update_progress_with_metadata(job_data, " Preparing channel notification...", 64.0)

                # Step 17: Sending notification (68%)
                await update_progress_with_metadata(job_data, " Sending channel notification...", 68.0)

                # Get API_KEY from environment or settings for channel notification
                api_key = getattr(settings, 'API_KEY', None)
                if api_key:
                    await gitlab_manager.send_channel_notification(
                        device_props,
                        repo_url,
                        str(job_data["dump_args"]["url"]),
                        is_whitelisted,
                        job_data.get("add_blacklist", False),
                        api_key
                    )

                # On successful completion
                repo_info = {"url": repo_url, "path": repo_path}
                job_data["metadata"]["repository"] = repo_info
                job_data["metadata"]["status"] = "completed"
                job_data["metadata"]["end_time"] = datetime.now(timezone.utc).isoformat()

                await update_progress_with_metadata(job_data, "Repository created successfully", 100.0)

                return {
                    "success": True,
                    "repository_url": repo_url,
                    "device_info": device_props,
                    "metadata": job_data["metadata"]
                }

            except Exception as e:
                console.print(f"[red]Error in inner processing for job {job_id}: {e}[/red]")

                # Enhanced error handling
                job_data["metadata"].update({
                    "status": "failed",
                    "end_time": datetime.now(timezone.utc).isoformat(),
                    "error_context": {
                        "message": str(e),
                        "current_step": job_data.get("progress", {}).get("current_step", "Unknown step"),
                        "last_successful_step": job_data["metadata"]["progress_history"][-1]["message"] if job_data["metadata"]["progress_history"] else "None",
                        "failure_time": datetime.now(timezone.utc).isoformat(),
                        "traceback": _sanitize_traceback(traceback.format_exc())
                    }
                })

                # Send failure notification using existing message queue system
                await _send_failure_notification(job_data, str(e))

                return {"success": False, "error": str(e), "metadata": job_data["metadata"]}

    except Exception as e:
        console.print(f"[red]Critical error processing job {job_id}: {e}[/red]")
        console.print_exception()

        # Enhanced error handling for critical errors
        job_data["metadata"].update({
            "status": "failed",
            "end_time": datetime.now(timezone.utc).isoformat(),
            "error_context": {
                "message": f"Critical error: {str(e)}",
                "current_step": "Critical failure",
                "last_successful_step": job_data["metadata"]["progress_history"][-1]["message"] if job_data["metadata"]["progress_history"] else "None",
                "failure_time": datetime.now(timezone.utc).isoformat(),
                "traceback": _sanitize_traceback(traceback.format_exc())
            }
        })

        # Send failure notification for any unhandled exceptions
        try:
            await _send_failure_notification(job_data, f"Critical error: {str(e)}")
        except Exception as notification_error:
            console.print(f"[red]Failed to send failure notification: {notification_error}[/red]")

        return {"success": False, "error": str(e), "metadata": job_data["metadata"]}
