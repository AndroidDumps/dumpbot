from typing import Any, Dict, Optional

from telegram.ext import ContextTypes

from dumpyarabot.schemas import AcceptOptionsState, ActiveJenkinsBuild, MockupState, PendingReview

# Check if Redis is available and configured
try:
    from dumpyarabot.config import settings
    if hasattr(settings, 'REDIS_URL') and settings.REDIS_URL:
        from dumpyarabot.redis_storage import ReviewStorage as RedisReviewStorage
        USE_REDIS = True
    else:
        USE_REDIS = False
except (ImportError, AttributeError):
    USE_REDIS = False


class ReviewStorage:
    """Data access layer for managing pending reviews with automatic Redis/bot_data selection."""

    @staticmethod
    def get_pending_reviews(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
        """Get all pending reviews."""
        if USE_REDIS:
            return RedisReviewStorage.get_pending_reviews(context)
        else:
            if "pending_reviews" not in context.bot_data:
                context.bot_data["pending_reviews"] = {}
            return context.bot_data["pending_reviews"]

    @staticmethod
    def get_pending_review(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> Optional[PendingReview]:
        """Get a specific pending review by request_id."""
        if USE_REDIS:
            return RedisReviewStorage.get_pending_review(context, request_id)
        else:
            reviews = ReviewStorage.get_pending_reviews(context)
            review_data = reviews.get(request_id)
            if review_data:
                if isinstance(review_data, dict):
                    return PendingReview(**review_data)
                else:
                    return review_data
            return None

    @staticmethod
    def store_pending_review(
        context: ContextTypes.DEFAULT_TYPE, review: PendingReview
    ) -> None:
        """Store a pending review."""
        if USE_REDIS:
            RedisReviewStorage.store_pending_review(context, review)
        else:
            reviews = ReviewStorage.get_pending_reviews(context)
            reviews[review.request_id] = review.model_dump()

    @staticmethod
    def remove_pending_review(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> bool:
        """Remove a pending review. Returns True if removed, False if not found."""
        if USE_REDIS:
            return RedisReviewStorage.remove_pending_review(context, request_id)
        else:
            reviews = ReviewStorage.get_pending_reviews(context)
            if request_id in reviews:
                del reviews[request_id]
                return True
            return False

    @staticmethod
    def get_options_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> AcceptOptionsState:
        """Get options state for a request_id, creating default if not exists."""
        if USE_REDIS:
            return RedisReviewStorage.get_options_state(context, request_id)
        else:
            if "options_states" not in context.bot_data:
                context.bot_data["options_states"] = {}

            states = context.bot_data["options_states"]
            if request_id not in states:
                states[request_id] = AcceptOptionsState().model_dump()

            return AcceptOptionsState(**states[request_id])

    @staticmethod
    def update_options_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str, options: AcceptOptionsState
    ) -> None:
        """Update options state for a request_id."""
        if USE_REDIS:
            RedisReviewStorage.update_options_state(context, request_id, options)
        else:
            if "options_states" not in context.bot_data:
                context.bot_data["options_states"] = {}

            context.bot_data["options_states"][request_id] = options.model_dump()

    @staticmethod
    def remove_options_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> None:
        """Remove options state for a request_id."""
        if USE_REDIS:
            RedisReviewStorage.remove_options_state(context, request_id)
        else:
            if (
                "options_states" in context.bot_data
                and request_id in context.bot_data["options_states"]
            ):
                del context.bot_data["options_states"][request_id]

    @staticmethod
    def get_mockup_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> MockupState:
        """Get mockup state for a request_id, creating default if not exists."""
        if USE_REDIS:
            return RedisReviewStorage.get_mockup_state(context, request_id)
        else:
            if "mockup_states" not in context.bot_data:
                context.bot_data["mockup_states"] = {}

            states = context.bot_data["mockup_states"]
            if request_id not in states:
                states[request_id] = MockupState(request_id=request_id).model_dump()

            return MockupState(**states[request_id])

    @staticmethod
    def update_mockup_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str, state: MockupState
    ) -> None:
        """Update mockup state for a request_id."""
        if USE_REDIS:
            RedisReviewStorage.update_mockup_state(context, request_id, state)
        else:
            if "mockup_states" not in context.bot_data:
                context.bot_data["mockup_states"] = {}

            context.bot_data["mockup_states"][request_id] = state.model_dump()

    @staticmethod
    def remove_mockup_state(
        context: ContextTypes.DEFAULT_TYPE, request_id: str
    ) -> None:
        """Remove mockup state for a request_id."""
        if USE_REDIS:
            RedisReviewStorage.remove_mockup_state(context, request_id)
        else:
            if (
                "mockup_states" in context.bot_data
                and request_id in context.bot_data["mockup_states"]
            ):
                del context.bot_data["mockup_states"][request_id]

    @staticmethod
    def get_active_builds(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
        """Get all active Jenkins builds."""
        if USE_REDIS:
            return RedisReviewStorage.get_active_builds(context)
        else:
            if "active_builds" not in context.bot_data:
                context.bot_data["active_builds"] = {}
            return context.bot_data["active_builds"]

    @staticmethod
    def store_active_build(
        context: ContextTypes.DEFAULT_TYPE, build: ActiveJenkinsBuild
    ) -> None:
        """Store an active Jenkins build."""
        if USE_REDIS:
            RedisReviewStorage.store_active_build(context, build)
        else:
            builds = ReviewStorage.get_active_builds(context)
            builds[build.build_id] = build.model_dump()

    @staticmethod
    def get_active_build(
        context: ContextTypes.DEFAULT_TYPE, build_id: str
    ) -> Optional[ActiveJenkinsBuild]:
        """Get an active Jenkins build by build_id."""
        if USE_REDIS:
            return RedisReviewStorage.get_active_build(context, build_id)
        else:
            builds = ReviewStorage.get_active_builds(context)
            build_data = builds.get(build_id)
            if build_data:
                if isinstance(build_data, dict):
                    return ActiveJenkinsBuild(**build_data)
                else:
                    return build_data
            return None

    @staticmethod
    def remove_active_build(
        context: ContextTypes.DEFAULT_TYPE, build_id: str
    ) -> bool:
        """Remove an active Jenkins build. Returns True if removed, False if not found."""
        if USE_REDIS:
            return RedisReviewStorage.remove_active_build(context, build_id)
        else:
            builds = ReviewStorage.get_active_builds(context)
            if build_id in builds:
                del builds[build_id]
                return True
            return False

