from typing import Any, Dict, Optional

import redis
from telegram.ext import ContextTypes

from dumpyarabot.config import settings
from dumpyarabot.schemas import AcceptOptionsState, MockupState, PendingReview


class RedisStorage:
    """Redis-based storage layer for persistent data across bot restarts."""

    _redis_client: Optional[redis.Redis] = None

    @classmethod
    def get_redis_client(cls) -> redis.Redis:
        """Get or create Redis client connection."""
        if cls._redis_client is None:
            cls._redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        return cls._redis_client

    @classmethod
    def _make_key(cls, key: str) -> str:
        """Create a prefixed Redis key."""
        return f"{settings.REDIS_KEY_PREFIX}{key}"

    @classmethod
    def get_pending_reviews(cls) -> Dict[str, Any]:
        """Get all pending reviews from Redis."""
        redis_client = cls.get_redis_client()
        reviews = {}
        for key in redis_client.scan_iter(match=cls._make_key("pending_reviews:*")):
            data = redis_client.get(key)
            if not data:
                continue
            review = PendingReview.model_validate_json(data)
            reviews[review.request_id] = review.model_dump()
        return reviews

    @classmethod
    def get_pending_review(cls, request_id: str) -> Optional[PendingReview]:
        """Get a specific pending review by request_id."""
        redis_client = cls.get_redis_client()
        key = cls._make_key(f"pending_reviews:{request_id}")
        data = redis_client.get(key)
        if not data:
            return None
        return PendingReview.model_validate_json(data)

    @classmethod
    def store_pending_review(cls, review: PendingReview) -> None:
        """Store a pending review."""
        redis_client = cls.get_redis_client()
        key = cls._make_key(f"pending_reviews:{review.request_id}")
        redis_client.set(key, review.model_dump_json())

    @classmethod
    def remove_pending_review(cls, request_id: str) -> bool:
        """Remove a pending review. Returns True if removed, False if not found."""
        redis_client = cls.get_redis_client()
        key = cls._make_key(f"pending_reviews:{request_id}")
        return bool(redis_client.delete(key))

    @classmethod
    def get_options_state(cls, request_id: str) -> AcceptOptionsState:
        """Get options state for a request_id, creating default if not exists."""
        redis_client = cls.get_redis_client()
        key = cls._make_key(f"options_states:{request_id}")
        data = redis_client.get(key)

        if not data:
            state = AcceptOptionsState()
            redis_client.set(key, state.model_dump_json())
            return state

        return AcceptOptionsState.model_validate_json(data)

    @classmethod
    def update_options_state(cls, request_id: str, options: AcceptOptionsState) -> None:
        """Update options state for a request_id."""
        redis_client = cls.get_redis_client()
        key = cls._make_key(f"options_states:{request_id}")
        redis_client.set(key, options.model_dump_json())

    @classmethod
    def remove_options_state(cls, request_id: str) -> None:
        """Remove options state for a request_id."""
        redis_client = cls.get_redis_client()
        key = cls._make_key(f"options_states:{request_id}")
        redis_client.delete(key)

    @classmethod
    def get_mockup_state(cls, request_id: str) -> MockupState:
        """Get mockup state for a request_id, creating default if not exists."""
        redis_client = cls.get_redis_client()
        key = cls._make_key(f"mockup_states:{request_id}")
        data = redis_client.get(key)

        if not data:
            state = MockupState(request_id=request_id)
            redis_client.set(key, state.model_dump_json())
            return state

        return MockupState.model_validate_json(data)

    @classmethod
    def update_mockup_state(cls, request_id: str, state: MockupState) -> None:
        """Update mockup state for a request_id."""
        redis_client = cls.get_redis_client()
        key = cls._make_key(f"mockup_states:{request_id}")
        redis_client.set(key, state.model_dump_json())

    @classmethod
    def remove_mockup_state(cls, request_id: str) -> None:
        """Remove mockup state for a request_id."""
        redis_client = cls.get_redis_client()
        key = cls._make_key(f"mockup_states:{request_id}")
        redis_client.delete(key)

    @classmethod
    def store_restart_message_info(cls, chat_id: int, message_id: int, user_mention: str) -> None:
        """Store restart message info for post-restart update."""
        try:
            redis_client = cls.get_redis_client()
            key = cls._make_key("restart_message_info")

            restart_info = {
                "chat_id": chat_id,
                "message_id": message_id,
                "user_mention": user_mention
            }

            redis_client.set(key, cls._serialize_dict(restart_info), ex=300)  # Expire after 5 minutes

        except Exception as e:
            from rich.console import Console
            console = Console()
            console.print(f"[red]Error storing restart message info: {e}[/red]")

    @classmethod
    def get_restart_message_info(cls) -> Optional[Dict[str, Any]]:
        """Get stored restart message info."""
        try:
            redis_client = cls.get_redis_client()
            key = cls._make_key("restart_message_info")

            data = redis_client.get(key)
            if data:
                return cls._deserialize_dict(data)
            return None

        except Exception as e:
            from rich.console import Console
            console = Console()
            console.print(f"[red]Error retrieving restart message info: {e}[/red]")
            return None

    @classmethod
    def clear_restart_message_info(cls) -> None:
        """Clear stored restart message info."""
        try:
            redis_client = cls.get_redis_client()
            key = cls._make_key("restart_message_info")
            redis_client.delete(key)

        except Exception as e:
            from rich.console import Console
            console = Console()
            console.print(f"[red]Error clearing restart message info: {e}[/red]")

    @staticmethod
    def _serialize_dict(data: Dict[str, Any]) -> str:
        """Serialize a simple dictionary to JSON."""
        import json
        return json.dumps(data)

    @staticmethod
    def _deserialize_dict(data: str) -> Dict[str, Any]:
        """Deserialize a simple dictionary from JSON."""
        import json
        return json.loads(data)


# Backward compatibility adapter that wraps RedisStorage with bot_data interface
class ReviewStorage:
    """Compatibility layer that adapts RedisStorage to the existing bot_data interface."""

    @staticmethod
    def get_pending_reviews(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
        """Get all pending reviews."""
        return RedisStorage.get_pending_reviews()

    @staticmethod
    def get_pending_review(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> Optional[PendingReview]:
        """Get a specific pending review by request_id."""
        return RedisStorage.get_pending_review(request_id)

    @staticmethod
    def store_pending_review(
        context: ContextTypes.DEFAULT_TYPE, review: PendingReview
    ) -> None:
        """Store a pending review."""
        RedisStorage.store_pending_review(review)

    @staticmethod
    def remove_pending_review(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> bool:
        """Remove a pending review. Returns True if removed, False if not found."""
        return RedisStorage.remove_pending_review(request_id)

    @staticmethod
    def get_options_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> AcceptOptionsState:
        """Get options state for a request_id, creating default if not exists."""
        return RedisStorage.get_options_state(request_id)

    @staticmethod
    def update_options_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str, options: AcceptOptionsState
    ) -> None:
        """Update options state for a request_id."""
        RedisStorage.update_options_state(request_id, options)

    @staticmethod
    def remove_options_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> None:
        """Remove options state for a request_id."""
        RedisStorage.remove_options_state(request_id)

    @staticmethod
    def get_mockup_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> MockupState:
        """Get mockup state for a request_id, creating default if not exists."""
        return RedisStorage.get_mockup_state(request_id)

    @staticmethod
    def update_mockup_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str, state: MockupState
    ) -> None:
        """Update mockup state for a request_id."""
        RedisStorage.update_mockup_state(request_id, state)

    @staticmethod
    def remove_mockup_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> None:
        """Remove mockup state for a request_id."""
        RedisStorage.remove_mockup_state(request_id)

