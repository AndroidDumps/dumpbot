import asyncio
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional, List

import redis.asyncio as redis
from pydantic import BaseModel
from rich.console import Console
from telegram import Bot
from telegram.error import RetryAfter, TelegramError, NetworkError
import telegram

from dumpyarabot.config import settings
from dumpyarabot.schemas import DumpJob, JobStatus

console = Console()


class MessageType(str, Enum):
    """Types of messages that can be queued."""
    COMMAND_REPLY = "command_reply"
    STATUS_UPDATE = "status_update"
    NOTIFICATION = "notification"
    CROSS_CHAT = "cross_chat"
    ERROR = "error"


class MessagePriority(str, Enum):
    """Message priority levels."""
    URGENT = "urgent"     # Errors, critical notifications
    HIGH = "high"         # Command replies, user-facing updates
    NORMAL = "normal"     # Status updates, progress reports
    LOW = "low"           # Background notifications, cleanup


class QueuedMessage(BaseModel):
    """Schema for messages in the Redis queue."""
    message_id: str
    type: MessageType
    priority: MessagePriority
    chat_id: int
    text: str
    parse_mode: str
    reply_to_message_id: Optional[int] = None
    reply_parameters: Optional[Dict[str, Any]] = None
    edit_message_id: Optional[int] = None
    delete_after: Optional[int] = None
    keyboard: Optional[Dict[str, Any]] = None
    disable_web_page_preview: Optional[bool] = None
    retry_count: int = 0
    max_retries: int = 3
    created_at: datetime
    scheduled_for: Optional[datetime] = None
    context: Dict[str, Any] = {}

    def __init__(self, **data):
        if "message_id" not in data:
            data["message_id"] = str(uuid.uuid4())
        if "created_at" not in data:
            data["created_at"] = datetime.utcnow()
        if "parse_mode" not in data or data.get("parse_mode") is None:
            data["parse_mode"] = settings.DEFAULT_PARSE_MODE
        super().__init__(**data)


# Rebuild the model to resolve any forward references
QueuedMessage.model_rebuild()

class MessageQueue:
    """Redis-based message queue for unified Telegram messaging."""

    def __init__(self):
        self._redis: Optional[redis.Redis] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._running = False
        self._bot: Optional[Bot] = None
        self._last_edit_times: Dict[str, datetime] = {}  # Track edit times by message_id

    async def _get_redis(self) -> redis.Redis:
        """Get or create Redis connection."""
        if self._redis is None:
            self._redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._redis

    def _make_queue_key(self, priority: MessagePriority) -> str:
        """Create Redis key for priority queue."""
        return f"{settings.REDIS_KEY_PREFIX}msg_queue:{priority.value}"

    async def publish(self, message: QueuedMessage) -> str:
        """Publish a message to the appropriate priority queue and return message_id."""
        redis_client = await self._get_redis()
        queue_key = self._make_queue_key(message.priority)

        # Serialize message
        message_json = message.model_dump_json()

        # Add to priority queue (LPUSH for FIFO with RPOP)
        await redis_client.lpush(queue_key, message_json)

        console.print(f"[green]Queued {message.type.value} message for chat {message.chat_id} (priority: {message.priority.value})[/green]")

        # Return the message_id for cases where we need to track it
        return message.message_id

    async def send_reply(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = settings.DEFAULT_PARSE_MODE,
        priority: MessagePriority = MessagePriority.HIGH,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send a reply message."""
        message = QueuedMessage(
            type=MessageType.COMMAND_REPLY,
            priority=priority,
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
            context=context or {}
        )
        await self.publish(message)

    async def send_status_update(
        self,
        chat_id: int,
        text: str,
        edit_message_id: Optional[int] = None,
        parse_mode: Optional[str] = settings.DEFAULT_PARSE_MODE,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send a status update message."""
        # Ensure parse_mode is always set to a valid value
        if parse_mode is None:
            parse_mode = settings.DEFAULT_PARSE_MODE
        message = QueuedMessage(
            type=MessageType.STATUS_UPDATE,
            priority=MessagePriority.NORMAL,
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            edit_message_id=edit_message_id,
            disable_web_page_preview=True,
            context=context or {}
        )
        await self.publish(message)

    async def send_cross_chat(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int,
        reply_to_chat_id: int,
        parse_mode: Optional[str] = settings.DEFAULT_PARSE_MODE,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send a cross-chat message with reply parameters."""
        reply_params = {
            "message_id": reply_to_message_id,
            "chat_id": reply_to_chat_id
        }

        message = QueuedMessage(
            type=MessageType.CROSS_CHAT,
            priority=MessagePriority.HIGH,
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_parameters=reply_params,
            context=context or {}
        )
        await self.publish(message)

    async def send_notification(
        self,
        chat_id: int,
        text: str,
        priority: MessagePriority = MessagePriority.URGENT,
        parse_mode: Optional[str] = settings.DEFAULT_PARSE_MODE,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send a notification message."""
        message = QueuedMessage(
            type=MessageType.NOTIFICATION,
            priority=priority,
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            context=context or {}
        )
        await self.publish(message)

    async def send_error(
        self,
        chat_id: int,
        text: str,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send an error message with urgent priority."""
        message = QueuedMessage(
            type=MessageType.ERROR,
            priority=MessagePriority.URGENT,
            chat_id=chat_id,
            text=text,
            parse_mode=settings.DEFAULT_PARSE_MODE,
            context=context or {}
        )
        await self.publish(message)

    class MessagePlaceholder:
        """Placeholder object that mimics a Telegram Message for compatibility."""
        def __init__(self, message_id: str, chat_id: int):
            self.message_id = message_id
            self.chat = type('Chat', (), {'id': chat_id})()

    async def publish_and_return_placeholder(
        self,
        message: QueuedMessage
    ) -> "MessageQueue.MessagePlaceholder":
        """Publish message and return a placeholder object for compatibility."""
        message_id = await self.publish(message)
        return self.MessagePlaceholder(message_id, message.chat_id)

    async def send_immediate_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = settings.DEFAULT_PARSE_MODE,
        reply_to_message_id: Optional[int] = None,
        disable_web_page_preview: bool = True
    ) -> "telegram.Message":
        """Send message directly via bot and return real Telegram Message object.

        This bypasses the queue entirely and provides immediate access to the real
        Telegram message ID for subsequent editing operations.

        Args:
            chat_id: The Telegram chat ID
            text: The message text
            parse_mode: Telegram parse mode (default: Markdown)
            reply_to_message_id: Optional message ID to reply to

        Returns:
            Real Telegram Message object with integer message_id

        Raises:
            Exception: If bot is not initialized
        """
        if not self._bot:
            raise Exception("Bot not initialized - cannot send immediate message")

        console.print(f"[blue]Sending immediate message to chat {chat_id} with parse_mode={parse_mode}[/blue]")

        message = await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
            disable_web_page_preview=disable_web_page_preview
        )

        console.print(f"[green]Sent immediate message {message.message_id} to chat {chat_id}[/green]")
        return message

    async def send_immediate_status_update(
        self,
        chat_id: int,
        text: str,
        context: Optional[Dict[str, Any]] = None
    ) -> "MessageQueue.MessagePlaceholder":
        """Send a status update message immediately and return a message placeholder for tracking.

        This method is used when you need to get a message reference immediately
        for later editing or tracking purposes.

        Args:
            chat_id: The Telegram chat ID
            text: The status message text
            context: Optional context for tracking

        Returns:
            MessagePlaceholder object with message_id for tracking
        """
        message = QueuedMessage(
            type=MessageType.STATUS_UPDATE,
            priority=MessagePriority.HIGH,  # Higher priority for immediate messages
            chat_id=chat_id,
            text=text,
            parse_mode=settings.DEFAULT_PARSE_MODE,
            context=context or {}
        )
        return await self.publish_and_return_placeholder(message)

    def set_bot(self, bot: Bot) -> None:
        """Set the Telegram bot instance."""
        self._bot = bot

    async def start_consumer(self) -> None:
        """Start the message consumer background task."""
        if self._consumer_task and not self._consumer_task.done():
            console.print("[yellow]Message consumer is already running[/yellow]")
            return

        self._running = True
        self._consumer_task = asyncio.create_task(self._consume_messages())
        console.print("[green]Message queue consumer started[/green]")

    async def stop_consumer(self) -> None:
        """Stop the message consumer."""
        self._running = False
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
        console.print("[yellow]Message queue consumer stopped[/yellow]")

    async def _consume_messages(self) -> None:
        """Main consumer loop that processes messages from Redis queues."""
        redis_client = await self._get_redis()

        # Priority order: URGENT -> HIGH -> NORMAL -> LOW
        priorities = [
            MessagePriority.URGENT,
            MessagePriority.HIGH,
            MessagePriority.NORMAL,
            MessagePriority.LOW
        ]

        last_message_time = datetime.utcnow()
        rate_limit_delay = 0

        while self._running:
            try:
                message_processed = False

                # Check each priority queue in order
                for priority in priorities:
                    queue_key = self._make_queue_key(priority)

                    # Try to get a message (non-blocking)
                    message_json = await redis_client.rpop(queue_key)
                    if message_json:
                        message = QueuedMessage.model_validate_json(message_json)
                        success = await self._process_message(message)

                        if success:
                            message_processed = True
                            last_message_time = datetime.utcnow()

                            # Implement basic rate limiting (30 messages/second max)
                            now = datetime.utcnow()
                            time_since_last = (now - last_message_time).total_seconds()
                            if time_since_last < 0.033:  # ~30 messages/second
                                rate_limit_delay = 0.033 - time_since_last
                                await asyncio.sleep(rate_limit_delay)
                        else:
                            # Re-queue failed message with incremented retry count
                            await self._handle_failed_message(message)

                        break  # Process one message at a time

                # If no message was processed, wait a bit before checking again
                if not message_processed:
                    await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                console.print("[yellow]Message consumer cancelled[/yellow]")
                break
            except Exception as e:
                console.print(f"[red]Error in message consumer: {e}[/red]")
                await asyncio.sleep(1)  # Wait before retrying

    async def _process_message(self, message: QueuedMessage) -> bool:
        """Process a single message."""
        if not self._bot:
            console.print("[red]Bot instance not set in MessageQueue[/red]")
            return False

        try:
            parse_mode_info = f" with parse_mode={message.parse_mode}" if message.parse_mode else " with NO parse_mode"
            console.print(f"[blue]Processing {message.type.value} message for chat {message.chat_id}{parse_mode_info}[/blue]")

            # Prepare common parameters
            kwargs = {
                "chat_id": message.chat_id,
                "text": message.text,
            }

            if message.parse_mode:
                kwargs["parse_mode"] = message.parse_mode
                message.parse_mode = settings.DEFAULT_PARSE_MODE

            if message.disable_web_page_preview is not None:
                kwargs["disable_web_page_preview"] = message.disable_web_page_preview

            if message.keyboard:
                # Handle InlineKeyboardMarkup if provided
                from telegram import InlineKeyboardMarkup
                # Reconstruct InlineKeyboardMarkup from dict
                kwargs["reply_markup"] = InlineKeyboardMarkup.de_json(message.keyboard, bot=self._bot)

            # Handle different message types
            if message.edit_message_id:
                # Edit existing message
                kwargs["message_id"] = message.edit_message_id
                del kwargs["chat_id"]  # edit_message_text uses chat_id differently
                kwargs["chat_id"] = message.chat_id
                await self._bot.edit_message_text(**kwargs)
            else:
                # Send new message
                if message.reply_parameters:
                    # Cross-chat reply
                    from telegram import ReplyParameters
                    kwargs["reply_parameters"] = ReplyParameters(
                        message_id=message.reply_parameters["message_id"],
                        chat_id=message.reply_parameters["chat_id"]
                    )
                elif message.reply_to_message_id:
                    kwargs["reply_to_message_id"] = message.reply_to_message_id

                sent_message = await self._bot.send_message(**kwargs)

                # Handle auto-delete if specified
                if message.delete_after:
                    asyncio.create_task(
                        self._auto_delete_message(message.chat_id, sent_message.message_id, message.delete_after)
                    )

            console.print(f"[green]Successfully processed {message.type.value} message[/green]")
            return True

        except RetryAfter as e:
            console.print(f"[yellow]Rate limited by Telegram API. Retry after {e.retry_after} seconds[/yellow]")
            # Re-queue the message with a delay
            message.scheduled_for = datetime.utcnow() + timedelta(seconds=e.retry_after)
            await self._requeue_message(message)
            return True  # Don't increment retry count for rate limits

        except NetworkError as e:
            console.print(f"[yellow]Network error processing message: {e}[/yellow]")
            # Simple retry with exponential backoff for network issues
            retry_delay = min(30 * (2 ** message.retry_count), 300)  # 30s, 60s, 120s, 240s, 300s max
            message.scheduled_for = datetime.utcnow() + timedelta(seconds=retry_delay)
            await self._requeue_message(message)
            return True  # Don't increment retry count for network issues

        except TelegramError as e:
            console.print(f"[red]Telegram API error processing message: {e}[/red]")
            return False

        except Exception as e:
            console.print(f"[red]Unexpected error processing message: {e}[/red]")
            return False

    async def _handle_failed_message(self, message: QueuedMessage) -> None:
        """Handle a failed message by retrying or moving to dead letter queue."""
        message.retry_count += 1

        if message.retry_count <= message.max_retries:
            console.print(f"[yellow]Retrying message {message.message_id} (attempt {message.retry_count})[/yellow]")
            # Add exponential backoff delay
            delay = min(2 ** message.retry_count, 300)  # Max 5 minutes
            message.scheduled_for = datetime.utcnow() + timedelta(seconds=delay)
            await self._requeue_message(message)
        else:
            console.print(f"[red]Message {message.message_id} exceeded max retries, moving to dead letter queue[/red]")
            await self._move_to_dead_letter_queue(message)

    async def _requeue_message(self, message: QueuedMessage) -> None:
        """Re-queue a message, potentially with a delay."""
        if message.scheduled_for and message.scheduled_for > datetime.utcnow():
            # Use Redis to schedule the message
            redis_client = await self._get_redis()
            delay_key = f"{settings.REDIS_KEY_PREFIX}delayed_messages"
            score = message.scheduled_for.timestamp()
            await redis_client.zadd(delay_key, {message.model_dump_json(): score})
        else:
            # Re-queue immediately
            await self.publish(message)

    async def _move_to_dead_letter_queue(self, message: QueuedMessage) -> None:
        """Move a failed message to the dead letter queue for manual review."""
        redis_client = await self._get_redis()
        dlq_key = f"{settings.REDIS_KEY_PREFIX}dead_letter_queue"
        await redis_client.lpush(dlq_key, message.model_dump_json())

    async def _auto_delete_message(self, chat_id: int, message_id: int, delay: int) -> None:
        """Auto-delete a message after the specified delay."""
        await asyncio.sleep(delay)
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            console.print(f"[green]Auto-deleted message {message_id} from chat {chat_id}[/green]")
        except Exception as e:
            console.print(f"[yellow]Failed to auto-delete message {message_id}: {e}[/yellow]")

    async def get_queue_stats(self) -> Dict[str, int]:
        """Get statistics about the message queues."""
        redis_client = await self._get_redis()
        stats = {}

        for priority in MessagePriority:
            queue_key = self._make_queue_key(priority)
            count = await redis_client.llen(queue_key)
            stats[priority.value] = count

        # Add dead letter queue stats
        dlq_key = f"{settings.REDIS_KEY_PREFIX}dead_letter_queue"
        stats["dead_letter"] = await redis_client.llen(dlq_key)

        return stats

    # ========== ARQ BRIDGE FUNCTIONALITY ==========
    # This section bridges to ARQ while preserving all Telegram messaging features

    def _should_throttle_edit(self, message_id: str, min_interval: float = 2.0) -> bool:
        """Check if message edit should be throttled based on rate limiting."""
        now = datetime.utcnow()
        last_edit = self._last_edit_times.get(message_id)

        if last_edit and (now - last_edit).total_seconds() < min_interval:
            return True

        self._last_edit_times[message_id] = now
        return False

    async def send_cross_chat_edit(
        self,
        chat_id: int,
        text: str,
        edit_message_id: int,
        reply_to_message_id: int,
        reply_to_chat_id: int,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send a cross-chat message edit with reply parameters."""
        reply_params = {
            "message_id": reply_to_message_id,
            "chat_id": reply_to_chat_id
        }

        message = QueuedMessage(
            type=MessageType.CROSS_CHAT,
            priority=MessagePriority.NORMAL,
            chat_id=chat_id,
            text=text,
            parse_mode=settings.DEFAULT_PARSE_MODE,
            edit_message_id=edit_message_id,
            reply_parameters=reply_params,
            disable_web_page_preview=True,
            context=context or {}
        )
        await self.publish(message)

    async def get_job_status(self, job_id: str) -> Optional[DumpJob]:
        """Enhanced ARQ status retrieval with rich metadata."""
        from dumpyarabot.arq_config import arq_pool

        arq_status = await arq_pool.get_job_status(job_id)
        if not arq_status:
            return None

        # Extract metadata from ARQ result if available
        result = arq_status.get("result", {})
        metadata = result.get("metadata", {})

        # Build enhanced DumpJob with metadata
        job_data = {
            "job_id": job_id,
            "status": self._arq_status_to_job_status(arq_status["status"]),
            "dump_args": {"url": metadata.get("telegram_context", {}).get("url", "")},
            "add_blacklist": False,
            "created_at": arq_status.get("enqueue_time"),
            "started_at": metadata.get("start_time"),
            "completed_at": metadata.get("end_time"),
            "worker_id": "arq_worker",
            "error_details": metadata.get("error_context", {}).get("message") if metadata.get("error_context") else None,
            "result_data": result,
            "progress": self._extract_current_progress(metadata),
            # Add rich metadata fields
            "device_info": metadata.get("device_info"),
            "repository": metadata.get("repository"),
            "telegram_context": metadata.get("telegram_context", {}),
            "progress_history": metadata.get("progress_history", [])
        }

        return DumpJob.model_validate(job_data)

    def _arq_status_to_job_status(self, arq_status: str) -> JobStatus:
        """Convert ARQ status to JobStatus enum."""
        status_mapping = {
            "queued": JobStatus.QUEUED,
            "in_progress": JobStatus.PROCESSING,
            "complete": JobStatus.COMPLETED,
            "not_found": JobStatus.FAILED,
            "deferred": JobStatus.QUEUED
        }
        return status_mapping.get(arq_status, JobStatus.FAILED)

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel an ARQ job."""
        from dumpyarabot.arq_config import arq_pool

        success = await arq_pool.cancel_job(job_id)

        if success:
            console.print(f"[green]Cancelled ARQ job {job_id}[/green]")
        else:
            console.print(f"[yellow]Could not cancel ARQ job {job_id}[/yellow]")

        return success

    async def get_job_queue_stats(self) -> Dict[str, Any]:
        """Get ARQ queue statistics."""
        from dumpyarabot.arq_config import arq_pool

        arq_stats = await arq_pool.get_queue_stats()

        # Convert to format expected by existing status commands
        return {
            "total_jobs": arq_stats.get("queue_length", 0),
            "queued_jobs": arq_stats.get("queue_length", 0),
            "active_workers": arq_stats.get("active_health_checks", 0),
            "status_breakdown": {
                "queued": arq_stats.get("queue_length", 0),
                "processing": 0,  # ARQ doesn't provide this directly
                "completed": 0,   # ARQ doesn't provide this directly
                "failed": 0,      # ARQ doesn't provide this directly
                "cancelled": 0    # ARQ doesn't provide this directly
            },
            "worker_keys": [],
            "arq_stats": arq_stats  # Include raw ARQ stats for debugging
        }

    # ========== METADATA ENHANCED METHODS ==========

    async def queue_dump_job_with_metadata(self, enhanced_job_data: Dict[str, Any]) -> str:
        """Queue a dump job with metadata support."""
        from dumpyarabot.arq_config import arq_pool, get_job_result_ttl

        job_data = enhanced_job_data

        # Enqueue to ARQ with metadata
        arq_job = await arq_pool.enqueue_job(
            "process_firmware_dump",
            job_data,
            job_id=enhanced_job_data["job_id"],
            result_ttl=get_job_result_ttl("running")  # Initial TTL
        )

        console.print(f"[green]Queued ARQ dump job {enhanced_job_data['job_id']} with metadata[/green]")
        return enhanced_job_data["job_id"]

    async def get_job_status(self, job_id: str) -> Optional[DumpJob]:
        """Enhanced ARQ status retrieval with rich metadata."""
        from dumpyarabot.arq_config import arq_pool

        arq_status = await arq_pool.get_job_status(job_id)
        if not arq_status:
            return None

        # Extract metadata from ARQ result if available
        result = arq_status.get("result", {})
        metadata = result.get("metadata", {})

        # Build enhanced DumpJob with metadata
        job_data = {
            "job_id": job_id,
            "status": self._arq_status_to_job_status(arq_status["status"]),
            "dump_args": {"url": metadata.get("telegram_context", {}).get("url", "")},
            "add_blacklist": False,
            "created_at": arq_status.get("enqueue_time"),
            "started_at": metadata.get("start_time"),
            "completed_at": metadata.get("end_time"),
            "worker_id": "arq_worker",
            "error_details": metadata.get("error_context", {}).get("message") if metadata.get("error_context") else None,
            "result_data": result,
            "progress": self._extract_current_progress(metadata),
            # Add rich metadata fields
            "device_info": metadata.get("device_info"),
            "repository": metadata.get("repository"),
            "telegram_context": metadata.get("telegram_context", {}),
            "progress_history": metadata.get("progress_history", [])
        }

        return DumpJob.model_validate(job_data)

    def _extract_current_progress(self, metadata: Dict) -> Optional[Dict]:
        """Extract current progress from metadata history."""
        history = metadata.get("progress_history", [])
        if history:
            latest = history[-1]
            return {
                "current_step": latest.get("message", "Unknown"),
                "percentage": latest.get("percentage", 0),
                "details": latest,
                "current_step_number": len(history)
            }
        return None

    async def get_active_jobs_with_metadata(self) -> List[DumpJob]:
        """Get active jobs with metadata (placeholder - ARQ doesn't provide this directly)."""
        # For now, return empty list - would need ARQ extension to track active jobs
        # This could be implemented by maintaining a separate Redis set of active job IDs
        return []

    async def get_recent_jobs_with_metadata(self, limit: int = 10) -> List[DumpJob]:
        """Get recent jobs with metadata (placeholder - ARQ doesn't provide this directly)."""
        # For now, return empty list - would need ARQ extension or separate tracking
        # This could be implemented by maintaining a Redis sorted set of recent jobs
        return []

    # Legacy methods (kept for backward compatibility during transition)
    async def get_next_job(self, worker_id: str) -> Optional[DumpJob]:
        """Legacy method - no longer used with ARQ workers."""
        console.print(f"[yellow]get_next_job called but ARQ handles worker management[/yellow]")
        return None

    async def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        progress: Optional[Dict[str, Any]] = None,
        error_details: Optional[str] = None,
        result_data: Optional[Dict[str, Any]] = None,
        job_data: Optional[DumpJob] = None
    ) -> bool:
        """Legacy method - ARQ handles job status internally."""
        console.print(f"[yellow]update_job_status called but ARQ manages job status internally[/yellow]")
        return True


# Global message queue instance
message_queue = MessageQueue()