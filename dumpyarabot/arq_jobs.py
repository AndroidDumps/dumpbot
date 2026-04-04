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
from urllib.parse import urlsplit, urlunsplit

from rich.console import Console

from dumpyarabot.config import settings
from dumpyarabot.firmware_downloader import FirmwareDownloader
from dumpyarabot.firmware_extractor import FirmwareExtractor
from dumpyarabot.gitlab_manager import GitLabManager
from dumpyarabot.schemas import DumpJob
from dumpyarabot.message_queue import message_queue
from dumpyarabot.property_extractor import PropertyExtractor
from dumpyarabot.aria2_manager import DownloadProgress
from dumpyarabot.message_formatting import format_comprehensive_progress_message, format_download_progress

console = Console()

# Patterns to sanitize from tracebacks to prevent credential exposure
_SENSITIVE_PATTERNS = [
    re.compile(r'(Bearer\s+)\S+', re.IGNORECASE),
    re.compile(r'(Authorization:\s*)\S+', re.IGNORECASE),
    re.compile(r'(token[=:]\s*)\S+', re.IGNORECASE),
    re.compile(r'(password[=:]\s*)\S+', re.IGNORECASE),
]
_URL_PATTERN = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)


def _sanitize_traceback(tb_str: str) -> str:
    """Remove sensitive tokens and credentials from traceback strings."""
    for pattern in _SENSITIVE_PATTERNS:
        tb_str = pattern.sub(r'\1[REDACTED]', tb_str)
    return _URL_PATTERN.sub(
        lambda match: _sanitize_url_for_log(match.group(0).rstrip(".,;:!?)\"]}'")),
        tb_str,
    )


def _sanitize_text(value: Any) -> str:
    """Sanitize arbitrary log text."""
    return _sanitize_traceback(str(value))


def _derive_last_successful_step(progress_history: list[Dict[str, Any]], failed_step: Optional[str] = None) -> Optional[str]:
    """Return the most recent successful step before a failed step, if known."""
    if not progress_history:
        return None

    if failed_step:
        for entry in reversed(progress_history):
            message = entry.get("message")
            if message and message != failed_step:
                return message
        return None

    latest = progress_history[-1].get("message")
    return latest if latest else None


def _sanitize_url_for_log(url_value: Any) -> str:
    """Redact credentials and query parameters from logged URLs."""
    url = str(url_value or "unknown")
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    try:
        hostname = parts.hostname or ""
        port = parts.port
        username = parts.username
    except ValueError:
        return url

    netloc = hostname
    if port:
        netloc = f"{netloc}:{port}"
    if username:
        netloc = f"[REDACTED]@{netloc}"

    sanitized = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return sanitized or url


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

    # Use cross-chat edits only for moderated requests that created a bot-owned status
    # message in the primary allowed chat. Direct dumps should always edit in-place.
    dump_args_initial_message_id = job_data["dump_args"].get("initial_message_id")
    is_moderated_request = bool(job_data.get("metadata", {}).get("telegram_context", {}).get("moderated_request"))

    primary_allowed_chat = settings.ALLOWED_CHATS[0] if settings.ALLOWED_CHATS else None

    if is_moderated_request and dump_args_initial_message_id and primary_allowed_chat is not None:
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


def _build_failure_log_text(job_data: Dict[str, Any]) -> str:
    """Assemble a plain-text failure log from job metadata."""
    lines = ["=== DUMPYARABOT JOB FAILURE LOG ==="]

    lines.append(f"Job ID:   {job_data.get('job_id', 'unknown')}")
    lines.append(f"Worker:   {job_data.get('worker_id', 'unknown')}")
    url = (job_data.get("dump_args") or {}).get("url", "unknown")
    lines.append(f"URL:      {_sanitize_url_for_log(url)}")

    metadata = job_data.get("metadata") or {}
    lines.append(f"Started:  {metadata.get('start_time', 'unknown')}")
    lines.append(f"Failed:   {metadata.get('end_time', 'unknown')}")

    lines.append("\n=== PROGRESS HISTORY ===")
    for entry in metadata.get("progress_history") or []:
        ts = entry.get("timestamp", "")
        msg = entry.get("message", "")
        try:
            pct_display = f"{float(entry.get('percentage', 0) or 0):.0f}%"
        except (TypeError, ValueError):
            pct_display = "?%"
        lines.append(f"[{ts}] ({pct_display}) {_sanitize_text(msg)}")

    error_ctx = metadata.get("error_context") or {}
    if error_ctx:
        lines.append("\n=== ERROR CONTEXT ===")
        lines.append(f"Failed at:       {_sanitize_text(error_ctx.get('current_step', 'unknown'))}")
        if error_ctx.get('last_successful_step'):
            lines.append(f"Last successful: {_sanitize_text(error_ctx['last_successful_step'])}")
        lines.append(f"Error message:   {_sanitize_text(error_ctx.get('message', 'unknown'))}")
        tb = error_ctx.get("traceback")
        if tb:
            lines.append("\n=== TRACEBACK (sanitized) ===")
            lines.append(_sanitize_traceback(tb))

    return "\n".join(lines)


async def _send_failure_notification(job_data: Dict[str, Any], error_details: str) -> None:
    """Send a failure notification using existing message queue - PRESERVING ALL TELEGRAM FEATURES."""

    try:
        # Recover last known progress from metadata history
        metadata = job_data.get("metadata") or {}
        progress_history = metadata.get("progress_history") or []
        last_progress = progress_history[-1] if progress_history else {}
        error_ctx = metadata.get("error_context") or {}
        last_step = error_ctx.get("current_step") or last_progress.get("message", "Unknown step")
        last_pct = last_progress.get("percentage", 0.0)

        failure_progress = {
            "current_step": "Failed",
            "total_steps": 25,
            "current_step_number": len(progress_history),
            "percentage": last_pct,
            "error_message": error_details
        }

        formatted_message = await format_comprehensive_progress_message(
            job_data,
            f" Failed at: {last_step}",
            failure_progress,
            metadata,
        )

        # PRESERVE: Check for required message context
        initial_message_id = job_data.get("initial_message_id")
        initial_chat_id = job_data.get("initial_chat_id")

        if not initial_message_id or not initial_chat_id:
            console.print(f"[red]ERROR: Job {job_data.get('job_id', 'unknown')} missing initial message reference! Cannot send failure update.[/red]")
            console.print(f"[red]Job data keys: {list(job_data.keys())}[/red]")
            return

        chat_id = initial_chat_id

        # Use cross-chat edits only for moderated requests that created a bot-owned
        # status message in the primary allowed chat.
        dump_args_initial_message_id = job_data.get("dump_args", {}).get("initial_message_id")
        is_moderated_request = bool(job_data.get("metadata", {}).get("telegram_context", {}).get("moderated_request"))

        primary_allowed_chat = settings.ALLOWED_CHATS[0] if settings.ALLOWED_CHATS else None

        if is_moderated_request and dump_args_initial_message_id and primary_allowed_chat is not None:
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

        # Send failure log as a text file for debugging
        try:
            log_text = _build_failure_log_text(job_data)
            log_bytes = log_text.encode("utf-8")
            job_id_short = str(job_data.get("job_id", "unknown"))[:8]
            filename = f"dump_failure_{job_id_short}.txt"
            target_chat = primary_allowed_chat if is_moderated_request and primary_allowed_chat is not None else chat_id
            await message_queue.send_document(
                chat_id=target_chat,
                content=log_bytes,
                filename=filename,
                caption="Failure log",
            )
        except Exception as log_err:
            console.print(f"[yellow]Could not queue failure log file: {log_err}[/yellow]")

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


async def update_progress_with_metadata(
    job_data: Dict[str, Any],
    step: str,
    percentage: float,
    extra_info: Optional[Dict[str, Any]] = None
) -> None:
    """Helper function for progress updates with metadata tracking."""
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

    # Add ARQ job metadata for tracking
    job_data["arq_job_id"] = ctx.get('job_id')
    job_data["worker_id"] = f"arq@{job_data['arq_job_id'][:8]}" if job_data["arq_job_id"] else "arq_worker"

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

                # Step 4: Starting download (15%)
                await update_progress_with_metadata(job_data, " Downloading firmware...", 15.0)

                # Create DumpJob object for components that need it
                dump_job = DumpJob.model_validate(job_data)

                # Download with live progress via aria2 RPC callback.
                # Download progress is mapped into the 15%–50% band of overall job progress.
                async def _on_download_progress(dp: DownloadProgress) -> None:
                    dl_pct = dp.percentage  # 0-100 within download
                    overall_pct = 15.0 + (dl_pct / 100.0) * 35.0  # map to 15%-50%
                    dl_info = format_download_progress(dp)
                    step_msg = f" Downloading firmware...\n{dl_info}"

                    progress_data = {
                        "current_step": step_msg,
                        "percentage": overall_pct,
                        "current_step_number": 4,
                        "total_steps": 25,
                    }
                    await _send_status_update(job_data, step_msg, progress_data, job_data.get("metadata"))

                # Use PeriodicTimerUpdate as a fallback for downloaders without
                # live progress (Google Drive, MediaFire, MEGA, wget fallback).
                # When aria2 RPC is active, the callback above sends updates instead.
                download_progress = {
                    "current_step": "Download",
                    "total_steps": 25,
                    "current_step_number": 4,
                    "percentage": 15.0,
                }
                async with PeriodicTimerUpdate(job_data, " Downloading firmware...", download_progress):
                    firmware_path, firmware_name = await downloader.download_firmware(
                        dump_job, on_progress=_on_download_progress
                    )

                # Step 5: Download completed (50%)
                await update_progress_with_metadata(job_data, " Firmware download completed", 50.0)

                # Step 6: Starting firmware extraction (52%)
                await update_progress_with_metadata(job_data, " Extracting firmware partitions...", 52.0)

                # Use periodic timer for extraction operation
                async with PeriodicTimerUpdate(job_data, " Extracting firmware partitions...", {"current_step": "Extract", "total_steps": 25, "current_step_number": 6, "percentage": 52.0}):
                    await extractor.extract_firmware(dump_job, firmware_path)

                # Step 7: Firmware extraction completed (56%)
                await update_progress_with_metadata(job_data, " Firmware extraction completed", 56.0)

                # Step 8: Process boot images (58%)
                await update_progress_with_metadata(job_data, " Processing boot images...", 58.0)
                await extractor.process_boot_images()

                # Step 9: Generate board-info.txt (60%)
                await update_progress_with_metadata(job_data, " Generating board-info.txt...", 60.0)
                await prop_extractor.generate_board_info()

                # Step 10: Generate all_files.txt (62%)
                await update_progress_with_metadata(job_data, " Generating all_files.txt...", 62.0)
                await prop_extractor.generate_all_files_list()

                # Step 11: Generate device tree (64%)
                await update_progress_with_metadata(job_data, " Generating device tree...", 64.0)
                await prop_extractor.generate_device_tree()

                # Step 12: Extracting device properties (66%)
                await update_progress_with_metadata(job_data, " Extracting device properties...", 66.0)
                device_props = await prop_extractor.extract_properties()
                job_data["metadata"]["device_info"] = device_props
                await update_progress_with_metadata(job_data, " Device analysis completed", 68.0)

                # Step 13: Checking/creating GitLab subgroup (70%)
                await update_progress_with_metadata(job_data, " Checking GitLab subgroup...", 70.0)

                # Step 14: Checking/creating GitLab project (72%)
                await update_progress_with_metadata(job_data, " Checking GitLab project...", 72.0)

                # Step 15: Setting up git repository (74%)
                await update_progress_with_metadata(job_data, " Creating GitLab repository...", 74.0)

                # Get DUMPER_TOKEN from environment or settings
                dumper_token = getattr(settings, 'DUMPER_TOKEN', None)
                if not dumper_token:
                    raise Exception("DUMPER_TOKEN not configured")

                # Use periodic timer for GitLab operation (longest post-download operation)
                gitlab_progress = {
                    "current_step": "GitLab",
                    "total_steps": 25,
                    "current_step_number": 15,
                    "percentage": 74.0,
                }
                async with PeriodicTimerUpdate(job_data, " Creating GitLab repository...", gitlab_progress):
                    repo_url, repo_path = await gitlab_manager.create_and_push_repository(
                        device_props,
                        dumper_token,
                        force=job_data["dump_args"].get("force", False),
                    )

                # Step 16: Preparing channel notification (88%)
                await update_progress_with_metadata(job_data, " Preparing channel notification...", 88.0)

                # Step 17: Sending notification (92%)
                await update_progress_with_metadata(job_data, " Sending channel notification...", 92.0)

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
                progress = job_data.get("progress") or {}
                metadata = job_data.get("metadata") or {}
                progress_history = metadata.get("progress_history") or []

                job_data["metadata"].update({
                    "status": "failed",
                    "end_time": datetime.now(timezone.utc).isoformat(),
                    "error_context": {
                        "message": str(e),
                        "current_step": progress_history[-1].get("message", "Unknown step") if progress_history else "Unknown step",
                        "last_successful_step": _derive_last_successful_step(
                            progress_history,
                            progress_history[-1].get("message") if progress_history else None,
                        ),
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
        metadata = job_data.get("metadata") or {}
        progress_history = metadata.get("progress_history") or []

        job_data["metadata"].update({
            "status": "failed",
            "end_time": datetime.now(timezone.utc).isoformat(),
            "error_context": {
                "message": f"Critical error: {str(e)}",
                "current_step": "Critical failure",
                "last_successful_step": _derive_last_successful_step(progress_history) or "None",
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
