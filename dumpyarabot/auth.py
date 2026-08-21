from typing import NamedTuple

from telegram import Update
from telegram.error import NetworkError
from telegram.ext import ContextTypes

from dumpyarabot.config import settings

# Admin status constants for consistency
ADMIN_STATUSES = ["administrator", "creator"]

# Shown when we couldn't reach Telegram to check admin status. This is an
# infrastructure error (timeout/proxy hiccup), NOT a permissions denial, so the
# wording must not imply the user lacks access.
VERIFICATION_FAILED_MESSAGE = (
    "Couldn't verify your admin status right now — that's a Telegram/network "
    "error on our end, not a permissions problem. Please try again in a moment."
)


class PermissionResult(NamedTuple):
    """Outcome of an admin-permission check.

    allowed:             the user may run the command.
    error:               human-readable reason when not allowed (for logs).
    verification_failed: we could not reach Telegram to check admin status, so
                         ``allowed`` is a fail-closed default rather than a real
                         denial. Callers should surface this as a transient
                         error ("try again"), not a permissions problem.
    """

    allowed: bool
    error: str | None = None
    verification_failed: bool = False


async def check_admin_permissions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    require_admin: bool = True
) -> PermissionResult:
    """Check if user has required permissions."""
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return PermissionResult(False, "Invalid chat or user")

    if chat.id not in settings.ALLOWED_CHATS:
        return PermissionResult(False, "Unauthorized chat")

    if require_admin:
        try:
            chat_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
        except NetworkError as e:
            # Timed out / proxy hiccup reaching Telegram. We genuinely don't know
            # whether the user is an admin, so treat it as a verification failure
            # rather than pretending they were denied. (TimedOut subclasses this.)
            return PermissionResult(
                False,
                f"Could not reach Telegram to verify admin status: {e}",
                verification_failed=True,
            )
        except Exception as e:  # noqa: BLE001 - any lookup failure = "couldn't verify", never a denial
            return PermissionResult(
                False,
                f"Could not verify admin status: {e}",
                verification_failed=True,
            )

        if chat_member.status not in ADMIN_STATUSES:
            return PermissionResult(False, "Admin permissions required")

    return PermissionResult(True)
