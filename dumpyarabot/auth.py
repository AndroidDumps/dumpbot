from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from dumpyarabot.config import settings

# Admin status constants for consistency
ADMIN_STATUSES = ["administrator", "creator"]


async def check_admin_permissions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    require_admin: bool = True
) -> Tuple[bool, Optional[str]]:
    """Check if user has required permissions. Returns (has_permission, error_message)."""
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return False, "Invalid chat or user"

    if chat.id not in settings.ALLOWED_CHATS:
        return False, "Unauthorized chat"

    if require_admin:
        try:
            chat_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
            if chat_member.status not in ADMIN_STATUSES:
                return False, "Admin permissions required"
        except Exception:
            return False, "Could not verify admin status"

    return True, None