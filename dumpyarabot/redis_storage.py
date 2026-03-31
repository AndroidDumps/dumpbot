import json
import re
from typing import Any, Dict, Optional

import redis.asyncio as redis
from telegram.ext import ContextTypes

from dumpyarabot.config import settings
from dumpyarabot.schemas import AcceptOptionsState, MockupState, PendingReview

# Regex for validating request IDs (8 hex chars)
_REQUEST_ID_RE = re.compile(r'^[a-f0-9]{8}$')


def _validate_request_id(request_id: str) -> str:
    """Validate request_id format to prevent Redis key injection."""
    if not _REQUEST_ID_RE.match(request_id):
        raise ValueError(f"Invalid request_id format: {request_id}")
    return request_id


class RedisStorage:
    """Redis-based storage layer for persistent data across bot restarts."""

    _redis_client: Optional[redis.Redis] = None

    @classmethod
    async def get_redis_client(cls) -> redis.Redis:
        """Get or create async Redis client connection."""
        if cls._redis_client is None:
            cls._redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        return cls._redis_client

    @classmethod
    def _make_key(cls, key: str) -> str:
        """Create a prefixed Redis key."""
        return f"{settings.REDIS_KEY_PREFIX}{key}"

    @classmethod
    async def get_pending_reviews(cls) -> Dict[str, Any]:
        """Get all pending reviews from Redis."""
        redis_client = await cls.get_redis_client()
        reviews = {}
        async for key in redis_client.scan_iter(match=cls._make_key("pending_reviews:*")):
            data = await redis_client.get(key)
            if not data:
                continue
            review = PendingReview.model_validate_json(data)
            reviews[review.request_id] = review.model_dump()
        return reviews

    @classmethod
    async def get_pending_review(cls, request_id: str) -> Optional[PendingReview]:
        """Get a specific pending review by request_id."""
        _validate_request_id(request_id)
        redis_client = await cls.get_redis_client()
        key = cls._make_key(f"pending_reviews:{request_id}")
        data = await redis_client.get(key)
        if not data:
            return None
        return PendingReview.model_validate_json(data)

    @classmethod
    async def store_pending_review(cls, review: PendingReview, ttl: int = 604800) -> None:
        """Store a pending review with TTL (default 7 days)."""
        redis_client = await cls.get_redis_client()
        key = cls._make_key(f"pending_reviews:{review.request_id}")
        await redis_client.set(key, review.model_dump_json(), ex=ttl)

    @classmethod
    async def remove_pending_review(cls, request_id: str) -> bool:
        """Remove a pending review. Returns True if removed, False if not found."""
        _validate_request_id(request_id)
        redis_client = await cls.get_redis_client()
        key = cls._make_key(f"pending_reviews:{request_id}")
        return bool(await redis_client.delete(key))

    @classmethod
    async def get_options_state(cls, request_id: str) -> AcceptOptionsState:
        """Get options state for a request_id, creating default if not exists (atomic)."""
        _validate_request_id(request_id)
        redis_client = await cls.get_redis_client()
        key = cls._make_key(f"options_states:{request_id}")

        # Atomic set-if-not-exists with default value
        default_state = AcceptOptionsState()
        was_set = await redis_client.setnx(key, default_state.model_dump_json())
        if was_set:
            await redis_client.expire(key, 604800)  # 7 day TTL

        data = await redis_client.get(key)
        return AcceptOptionsState.model_validate_json(data)

    @classmethod
    async def update_options_state(cls, request_id: str, options: AcceptOptionsState) -> None:
        """Update options state for a request_id."""
        _validate_request_id(request_id)
        redis_client = await cls.get_redis_client()
        key = cls._make_key(f"options_states:{request_id}")
        await redis_client.set(key, options.model_dump_json(), ex=604800)

    @classmethod
    async def remove_options_state(cls, request_id: str) -> None:
        """Remove options state for a request_id."""
        _validate_request_id(request_id)
        redis_client = await cls.get_redis_client()
        key = cls._make_key(f"options_states:{request_id}")
        await redis_client.delete(key)

    @classmethod
    async def get_mockup_state(cls, request_id: str) -> MockupState:
        """Get mockup state for a request_id, creating default if not exists (atomic)."""
        _validate_request_id(request_id)
        redis_client = await cls.get_redis_client()
        key = cls._make_key(f"mockup_states:{request_id}")

        # Atomic set-if-not-exists with default value
        default_state = MockupState(request_id=request_id)
        was_set = await redis_client.setnx(key, default_state.model_dump_json())
        if was_set:
            await redis_client.expire(key, 604800)  # 7 day TTL

        data = await redis_client.get(key)
        return MockupState.model_validate_json(data)

    @classmethod
    async def update_mockup_state(cls, request_id: str, state: MockupState) -> None:
        """Update mockup state for a request_id."""
        _validate_request_id(request_id)
        redis_client = await cls.get_redis_client()
        key = cls._make_key(f"mockup_states:{request_id}")
        await redis_client.set(key, state.model_dump_json(), ex=604800)

    @classmethod
    async def remove_mockup_state(cls, request_id: str) -> None:
        """Remove mockup state for a request_id."""
        _validate_request_id(request_id)
        redis_client = await cls.get_redis_client()
        key = cls._make_key(f"mockup_states:{request_id}")
        await redis_client.delete(key)

    @classmethod
    async def store_restart_message_info(cls, chat_id: int, message_id: int, user_mention: str) -> None:
        """Store restart message info for post-restart update."""
        try:
            redis_client = await cls.get_redis_client()
            key = cls._make_key("restart_message_info")

            restart_info = {
                "chat_id": chat_id,
                "message_id": message_id,
                "user_mention": user_mention
            }

            await redis_client.set(key, json.dumps(restart_info), ex=300)  # Expire after 5 minutes

        except Exception as e:
            from rich.console import Console
            console = Console()
            console.print(f"[red]Error storing restart message info: {e}[/red]")

    @classmethod
    async def get_restart_message_info(cls) -> Optional[Dict[str, Any]]:
        """Get stored restart message info."""
        try:
            redis_client = await cls.get_redis_client()
            key = cls._make_key("restart_message_info")

            data = await redis_client.get(key)
            if data:
                return json.loads(data)
            return None

        except Exception as e:
            from rich.console import Console
            console = Console()
            console.print(f"[red]Error retrieving restart message info: {e}[/red]")
            return None

    @classmethod
    async def clear_restart_message_info(cls) -> None:
        """Clear stored restart message info."""
        try:
            redis_client = await cls.get_redis_client()
            key = cls._make_key("restart_message_info")
            await redis_client.delete(key)

        except Exception as e:
            from rich.console import Console
            console = Console()
            console.print(f"[red]Error clearing restart message info: {e}[/red]")


# Backward compatibility adapter that wraps RedisStorage with bot_data interface
class ReviewStorage:
    """Compatibility layer that adapts RedisStorage to the existing bot_data interface."""

    @staticmethod
    async def get_pending_reviews(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
        """Get all pending reviews."""
        return await RedisStorage.get_pending_reviews()

    @staticmethod
    async def get_pending_review(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> Optional[PendingReview]:
        """Get a specific pending review by request_id."""
        return await RedisStorage.get_pending_review(request_id)

    @staticmethod
    async def store_pending_review(
        context: ContextTypes.DEFAULT_TYPE, review: PendingReview
    ) -> None:
        """Store a pending review."""
        await RedisStorage.store_pending_review(review)

    @staticmethod
    async def remove_pending_review(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> bool:
        """Remove a pending review. Returns True if removed, False if not found."""
        return await RedisStorage.remove_pending_review(request_id)

    @staticmethod
    async def get_options_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> AcceptOptionsState:
        """Get options state for a request_id, creating default if not exists."""
        return await RedisStorage.get_options_state(request_id)

    @staticmethod
    async def update_options_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str, options: AcceptOptionsState
    ) -> None:
        """Update options state for a request_id."""
        await RedisStorage.update_options_state(request_id, options)

    @staticmethod
    async def remove_options_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> None:
        """Remove options state for a request_id."""
        await RedisStorage.remove_options_state(request_id)

    @staticmethod
    async def get_mockup_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> MockupState:
        """Get mockup state for a request_id, creating default if not exists."""
        return await RedisStorage.get_mockup_state(request_id)

    @staticmethod
    async def update_mockup_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str, state: MockupState
    ) -> None:
        """Update mockup state for a request_id."""
        await RedisStorage.update_mockup_state(request_id, state)

    @staticmethod
    async def remove_mockup_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> None:
        """Remove mockup state for a request_id."""
        await RedisStorage.remove_mockup_state(request_id)
