import random
from typing import TYPE_CHECKING, Any, Optional

from telegram import Chat, Message, Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from telegram import InlineKeyboardMarkup

from dumpyarabot.config import (CALLBACK_ACCEPT, CALLBACK_CANCEL_REQUEST,
                                CALLBACK_REJECT,
                                CALLBACK_RESTART_CANCEL, CALLBACK_RESTART_CONFIRM,
                                CALLBACK_SUBMIT_ACCEPTANCE, CALLBACK_TOGGLE_ALT,
                                CALLBACK_TOGGLE_FORCE, CALLBACK_TOGGLE_PRIVDUMP)
from dumpyarabot.schemas import AcceptOptionsState, MockupState, PendingReview
from dumpyarabot.storage import ReviewStorage
from dumpyarabot.ui import (REVIEW_TEMPLATE, create_options_keyboard,
                            create_review_keyboard)
from dumpyarabot.utils import generate_request_id
from dumpyarabot.config import settings

# Import main handlers to avoid duplication
from dumpyarabot import moderated_handlers

# Mockup-specific callback prefixes for reset/back/delete functionality
CALLBACK_MOCKUP_RESET = "mockup_reset_"
CALLBACK_MOCKUP_BACK = "mockup_back_"
CALLBACK_MOCKUP_DELETE = "mockup_delete_"

# Funny randomized data for testing
FUNNY_USERNAMES = [
    "FlashMaster2000",
    "FirmwareNinja",
    "ROMAddict",
    "BootloopSurvivor",
    "APKHunter",
    "DebugDuck",
    "KernelPanic",
    "SystemUIGuru",
    "DalvikDreamer",
    "ADBWizard",
    "RecoveryHero",
    "ModemMage",
    "PartitionPirate",
    "FastbootFan",
]

FUNNY_URLS = [
    "https://totally-legit-firmware.example.com/super_secret_rom.zip",
    "https://downloads.sketchy-site.net/leaked_beta_v42.0.zip",
    "https://mirror.questionable-cdn.org/definitely_not_malware.bin",
    "https://files.random-uploader.io/my_custom_rom_please_flash.tar.gz",
    "https://mega.nz/file/ABC123/totally_safe_firmware_trust_me",
    "https://drive.google.com/uc?id=fake_but_looks_real_firmware",
    "https://mediafire.com/file/xyz789/experimental_rom_v999.zip",
    "https://pixeldrain.com/u/mockup123",
]

FUNNY_MESSAGES = [
    "#request https://example.com/firmware.zip please dump this new OnePlus ROM!",
    "#request https://example.com/rom.zip Found this cool ROM on XDA, can you dump it? Thanks!",
    "#request https://example.com/firmware.bin Hey guys, need this dumped ASAP for my project ",
    "#request https://example.com/update.zip Can someone please dump this Samsung firmware? Much appreciated!",
    "#request https://example.com/rom.tar.gz #request this leaked Pixel ROM, looks promising ",
    "#request https://example.com/firmware.zip Urgent! Need this Xiaomi ROM dumped for development work",
    "#request https://example.com/system.zip Please dump this custom ROM, want to check the vendor blobs",
    "#request https://example.com/ota.zip #request can you dump this OTA? It has some interesting changes",
]


async def _renew_expired_mockup_session(
    context: ContextTypes.DEFAULT_TYPE, request_id: str, controls_message_id: int, chat_id: int
) -> tuple[PendingReview, MockupState]:
    """Seamlessly renew an expired mockup session by updating existing review message."""
    fake_username = random.choice(FUNNY_USERNAMES)
    fake_url = random.choice(FUNNY_URLS)

    # Try to find the existing review message ID from the control message's context
    # In mockup sessions, review message ID is typically controls_message_id - 1
    # But we should search for the actual review message more carefully
    review_message_id = controls_message_id - 1  # Assume it's the previous message

    # Create fresh mockup review data pointing to existing review message
    mockup_review = PendingReview(
        request_id=request_id,
        original_chat_id=chat_id,
        original_message_id=controls_message_id,
        requester_id=99999999,  # Fake user ID
        requester_username=fake_username,
        url=fake_url,
        review_chat_id=chat_id,  # Same chat for mockup
        review_message_id=review_message_id,  # Point to existing review message
        submission_confirmation_message_id=None,
    )

    # Store the renewed mockup review and initialize state
    await ReviewStorage.store_pending_review(context, mockup_review)
    mockup_state = MockupState(
        request_id=request_id,
        current_menu="initial",
        original_command_message_id=controls_message_id,
    )
    await ReviewStorage.update_mockup_state(context, request_id, mockup_state)

    # Initialize options state
    options_state = AcceptOptionsState()
    await ReviewStorage.update_options_state(context, request_id, options_state)

    return mockup_review, mockup_state


async def mockup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the /mockup command that creates a fake moderated request flow."""
    chat: Optional[Chat] = update.effective_chat
    message: Optional[Message] = update.effective_message

    if not chat or not message:
        return

    # Generate mockup data
    request_id = generate_request_id()
    fake_username = random.choice(FUNNY_USERNAMES)
    fake_url = random.choice(FUNNY_URLS)
    fake_message = random.choice(FUNNY_MESSAGES)

    # Store mockup data in bot storage for state management
    mockup_review = PendingReview(
        request_id=request_id,
        original_chat_id=chat.id,
        original_message_id=message.message_id,  # Use the /mockup command message
        requester_id=99999999,  # Fake user ID
        requester_username=fake_username,
        url=fake_url,
        review_chat_id=chat.id,  # Same chat for mockup
        review_message_id=0,  # Will be updated after sending
        submission_confirmation_message_id=None,
    )

    # Store the mockup review and initialize state
    await ReviewStorage.store_pending_review(context, mockup_review)
    mockup_state = MockupState(
        request_id=request_id,
        current_menu="initial",
        original_command_message_id=message.message_id,
    )
    await ReviewStorage.update_mockup_state(context, request_id, mockup_state)

    # Create production-like review message
    review_text = REVIEW_TEMPLATE.format(
        username=fake_username, url=fake_url, request_id=request_id, original_message=fake_message
    )

    # Send the first message (production-like review message) via queue
    # Note: For mockups, we still need the message object, so we'll use a direct call here
    # but add a TODO to potentially enhance message_queue to return message objects
    review_message = await context.bot.send_message(
        chat_id=chat.id,
        text=review_text,
        reply_markup=create_review_keyboard(request_id),
        disable_web_page_preview=True,
    )

    # Update the stored review with the actual message ID
    mockup_review.review_message_id = review_message.message_id
    await ReviewStorage.store_pending_review(context, mockup_review)

    # Send compact control panel
    control_text = f"Mockup Controls ({request_id})"

    await context.bot.send_message(
        chat_id=chat.id,
        text=control_text,
        reply_markup=_create_compact_controls_keyboard(request_id),
    )


async def handle_mockup_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle mockup-specific callback queries for reset and back functionality."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    callback_data = query.data

    if callback_data.startswith(CALLBACK_MOCKUP_RESET):
        await _handle_mockup_reset(query, context, callback_data)
    elif callback_data.startswith(CALLBACK_MOCKUP_BACK):
        await _handle_mockup_back(query, context, callback_data)
    elif callback_data.startswith(CALLBACK_MOCKUP_DELETE):
        await _handle_mockup_delete(query, context, callback_data)


async def _handle_mockup_reset(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Reset the mockup to initial state (Accept/Reject buttons)."""
    request_id = callback_data[len(CALLBACK_MOCKUP_RESET) :]

    pending_review = await ReviewStorage.get_pending_review(context, request_id)
    if not pending_review:
        # Seamlessly renew expired mockup session
        pending_review, mockup_state = await _renew_expired_mockup_session(
            context, request_id, query.message.message_id, query.message.chat.id
        )

        # Update existing review message with fresh content and working buttons
        review_text = REVIEW_TEMPLATE.format(
            username=pending_review.requester_username,
            url=pending_review.url,
            request_id=request_id,
            original_message="[Mockup] Original message content",
        )

        try:
            await context.bot.edit_message_text(
                chat_id=pending_review.review_chat_id,
                message_id=pending_review.review_message_id,
                text=review_text,
                reply_markup=create_review_keyboard(request_id),
                disable_web_page_preview=True,
                parse_mode=settings.DEFAULT_PARSE_MODE,
            )
            success_message = " Session renewed - review message updated"
        except Exception:
            # If we can't find the review message, just note that session was renewed
            success_message = " Session renewed - ready to continue"

        # Update control message to show session is renewed
        await query.edit_message_text(
            success_message,
            reply_markup=_create_compact_controls_keyboard(request_id),
        )
        return

    # Reset options state to defaults
    options_state = AcceptOptionsState()
    await ReviewStorage.update_options_state(context, request_id, options_state)

    # Get the review message and reset it to initial state
    try:
        review_text = REVIEW_TEMPLATE.format(
            username=pending_review.requester_username,
            url=pending_review.url,
            request_id=request_id,
            original_message="[Mockup] Original message content from reset function",
        )

        await context.bot.edit_message_text(
            chat_id=pending_review.review_chat_id,
            message_id=pending_review.review_message_id,
            text=review_text,
            reply_markup=create_review_keyboard(request_id),
            disable_web_page_preview=True,
            parse_mode=settings.DEFAULT_PARSE_MODE,
        )

        await query.edit_message_text(
            " Reset complete!",
            reply_markup=_create_compact_controls_keyboard(request_id),
        )
    except Exception as e:
        await query.edit_message_text(f" Error resetting mockup: {str(e)}")


async def _handle_mockup_back(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Navigate back one menu level based on current state."""
    request_id = callback_data[len(CALLBACK_MOCKUP_BACK) :]

    pending_review = await ReviewStorage.get_pending_review(context, request_id)
    mockup_state = await ReviewStorage.get_mockup_state(context, request_id)

    if not pending_review or not mockup_state:
        # Seamlessly renew expired mockup session
        pending_review, mockup_state = await _renew_expired_mockup_session(
            context, request_id, query.message.message_id, query.message.chat.id
        )

        # Update existing review message with fresh content and working buttons
        review_text = REVIEW_TEMPLATE.format(
            username=pending_review.requester_username,
            url=pending_review.url,
            request_id=request_id,
            original_message="[Mockup] Original message content",
        )

        try:
            await context.bot.edit_message_text(
                chat_id=pending_review.review_chat_id,
                message_id=pending_review.review_message_id,
                text=review_text,
                reply_markup=create_review_keyboard(request_id),
                disable_web_page_preview=True,
                parse_mode=settings.DEFAULT_PARSE_MODE,
            )
            success_message = " Session renewed - review message updated"
        except Exception:
            # If we can't find the review message, just note that session was renewed
            success_message = " Session renewed - ready to continue"

        # Update control message to show session is renewed
        await query.edit_message_text(
            success_message,
            reply_markup=_create_compact_controls_keyboard(request_id),
        )
        return

    try:
        if mockup_state.current_menu == "options":
            # Go back from options to initial (Accept/Reject)
            mockup_state.current_menu = "initial"
            await ReviewStorage.update_mockup_state(context, request_id, mockup_state)

            review_text = REVIEW_TEMPLATE.format(
                username=pending_review.requester_username,
                url=pending_review.url,
                request_id=request_id,
                original_message="[Mockup] Original message content",
            )

            await context.bot.edit_message_text(
                chat_id=pending_review.review_chat_id,
                message_id=pending_review.review_message_id,
                text=review_text,
                reply_markup=create_review_keyboard(request_id),
                disable_web_page_preview=True,
                parse_mode=settings.DEFAULT_PARSE_MODE,
            )

            await query.edit_message_text(
                "Returned to Accept/Reject",
                reply_markup=_create_compact_controls_keyboard(request_id),
            )
        elif mockup_state.current_menu == "completed":
            # Go back from completed to options
            mockup_state.current_menu = "options"
            await ReviewStorage.update_mockup_state(context, request_id, mockup_state)

            options_state = await ReviewStorage.get_options_state(context, request_id)

            await context.bot.edit_message_text(
                chat_id=pending_review.review_chat_id,
                message_id=pending_review.review_message_id,
                text=f" Configure options for request {request_id}\nURL: {pending_review.url}",
                reply_markup=create_options_keyboard(request_id, options_state),
                disable_web_page_preview=True,
            )

            await query.edit_message_text(
                "Returned to Options",
                reply_markup=_create_compact_controls_keyboard(request_id),
            )
        elif mockup_state.current_menu == "rejected":
            # Go back from rejected to initial (Accept/Reject)
            mockup_state.current_menu = "initial"
            await ReviewStorage.update_mockup_state(context, request_id, mockup_state)

            review_text = REVIEW_TEMPLATE.format(
                username=pending_review.requester_username,
                url=pending_review.url,
                request_id=request_id,
                original_message="[Mockup] Original message content",
            )

            await context.bot.edit_message_text(
                chat_id=pending_review.review_chat_id,
                message_id=pending_review.review_message_id,
                text=review_text,
                reply_markup=create_review_keyboard(request_id),
                disable_web_page_preview=True,
                parse_mode=settings.DEFAULT_PARSE_MODE,
            )

            await query.edit_message_text(
                "Returned to Accept/Reject",
                reply_markup=_create_compact_controls_keyboard(request_id),
            )
        elif mockup_state.current_menu == "cancelled":
            # Go back from cancelled to initial (Accept/Reject)
            mockup_state.current_menu = "initial"
            await ReviewStorage.update_mockup_state(context, request_id, mockup_state)

            review_text = REVIEW_TEMPLATE.format(
                username=pending_review.requester_username,
                url=pending_review.url,
                request_id=request_id,
                original_message="[Mockup] Original message content",
            )

            await context.bot.edit_message_text(
                chat_id=pending_review.review_chat_id,
                message_id=pending_review.review_message_id,
                text=review_text,
                reply_markup=create_review_keyboard(request_id),
                disable_web_page_preview=True,
                parse_mode=settings.DEFAULT_PARSE_MODE,
            )

            await query.edit_message_text(
                "Returned to Accept/Reject",
                reply_markup=_create_compact_controls_keyboard(request_id),
            )
        else:
            # Already at initial state
            await query.edit_message_text(
                "Already at initial state",
                reply_markup=_create_compact_controls_keyboard(request_id),
            )

    except Exception as e:
        error_message = str(e)
        if "Message is not modified" in error_message:
            # Auto-reset mockup when navigation breaks due to identical content
            await _handle_mockup_reset(query, context, callback_data)
        else:
            await query.edit_message_text(f" Error navigating back: {error_message}")


async def _handle_mockup_delete(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Delete all mockup messages and clean up storage."""
    request_id = callback_data[len(CALLBACK_MOCKUP_DELETE) :]

    pending_review = await ReviewStorage.get_pending_review(context, request_id)
    mockup_state = await ReviewStorage.get_mockup_state(context, request_id)

    if not pending_review or not mockup_state:
        # Seamlessly renew expired mockup session
        pending_review, mockup_state = await _renew_expired_mockup_session(
            context, request_id, query.message.message_id, query.message.chat.id
        )

        # Update existing review message with fresh content and working buttons
        review_text = REVIEW_TEMPLATE.format(
            username=pending_review.requester_username,
            url=pending_review.url,
            request_id=request_id,
            original_message="[Mockup] Original message content",
        )

        try:
            await context.bot.edit_message_text(
                chat_id=pending_review.review_chat_id,
                message_id=pending_review.review_message_id,
                text=review_text,
                reply_markup=create_review_keyboard(request_id),
                disable_web_page_preview=True,
                parse_mode=settings.DEFAULT_PARSE_MODE,
            )
            success_message = " Session renewed - review message updated"
        except Exception:
            # If we can't find the review message, just note that session was renewed
            success_message = " Session renewed - ready to continue"

        # Update control message to show session is renewed
        await query.edit_message_text(
            success_message,
            reply_markup=_create_compact_controls_keyboard(request_id),
        )
        return

    try:
        # Try to delete the original /mockup command message (may fail if no permissions)
        try:
            await context.bot.delete_message(
                chat_id=pending_review.original_chat_id,
                message_id=mockup_state.original_command_message_id,
            )
        except Exception:
            # Ignore if we can't delete the command message
            pass

        # Delete the review message (bot's own message)
        try:
            await context.bot.delete_message(
                chat_id=pending_review.review_chat_id,
                message_id=pending_review.review_message_id,
            )
        except Exception:
            # Ignore if we can't delete the review message
            pass

        # Clean up storage before deleting controls message
        await ReviewStorage.remove_pending_review(context, request_id)
        await ReviewStorage.remove_options_state(context, request_id)
        await ReviewStorage.remove_mockup_state(context, request_id)

        # Delete the controls message (this one) - must be last
        await query.delete_message()

    except Exception as e:
        # Try to show error, but don't fail if the message is already deleted
        try:
            await query.edit_message_text(f" Error deleting messages: {str(e)}")
        except Exception:
            # If we can't edit the message, just clean up storage silently
            try:
                await ReviewStorage.remove_pending_review(context, request_id)
                await ReviewStorage.remove_options_state(context, request_id)
                await ReviewStorage.remove_mockup_state(context, request_id)
            except Exception:
                pass


def _create_compact_controls_keyboard(request_id: str) -> "InlineKeyboardMarkup":
    """Create compact mockup control buttons side by side."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = [
        [
            InlineKeyboardButton(
                " Reset", callback_data=f"{CALLBACK_MOCKUP_RESET}{request_id}"
            ),
            InlineKeyboardButton(
                "Back", callback_data=f"{CALLBACK_MOCKUP_BACK}{request_id}"
            ),
            InlineKeyboardButton(
                " Delete", callback_data=f"{CALLBACK_MOCKUP_DELETE}{request_id}"
            ),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# Enhanced callback handler that integrates with existing moderated system
async def handle_enhanced_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Enhanced callback handler that supports both production and mockup callbacks."""
    query = update.callback_query
    if not query or not query.data:
        return

    callback_data = query.data

    # Handle mockup-specific callbacks
    if (
        callback_data.startswith(CALLBACK_MOCKUP_RESET)
        or callback_data.startswith(CALLBACK_MOCKUP_BACK)
        or callback_data.startswith(CALLBACK_MOCKUP_DELETE)
    ):
        await handle_mockup_callback(update, context)
        return

    # For all other callbacks, use existing logic from moderated_handlers
    await query.answer()

    # Parse callback_data to determine action type
    if callback_data.startswith(CALLBACK_ACCEPT):
        await _handle_accept_callback_with_mockup_state(query, context, callback_data)
    elif callback_data.startswith(CALLBACK_REJECT):
        await _handle_reject_callback_with_mockup_state(query, context, callback_data)
    elif callback_data.startswith(CALLBACK_TOGGLE_ALT):
        await _handle_toggle_callback_with_mockup_state(query, context, callback_data, "alt")
    elif callback_data.startswith(CALLBACK_TOGGLE_FORCE):
        await _handle_toggle_callback_with_mockup_state(query, context, callback_data, "force")
    elif callback_data.startswith(CALLBACK_TOGGLE_PRIVDUMP):
        await _handle_toggle_callback_with_mockup_state(query, context, callback_data, "privdump")
    elif callback_data.startswith(CALLBACK_CANCEL_REQUEST):
        await _handle_cancel_callback_with_mockup_state(query, context, callback_data)
    elif callback_data.startswith(CALLBACK_SUBMIT_ACCEPTANCE):
        await _handle_submit_callback_with_mockup_state(query, context, callback_data)
    elif callback_data.startswith(CALLBACK_RESTART_CONFIRM) or callback_data.startswith(CALLBACK_RESTART_CANCEL):
        # Import restart handler here to avoid circular imports
        from dumpyarabot.handlers import handle_restart_callback
        await handle_restart_callback(update, context)


# Mockup-aware versions that handle state + delegate to main handlers
async def _handle_accept_callback_with_mockup_state(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Handle accept button with mockup state management."""
    request_id = callback_data[len(CALLBACK_ACCEPT) :]

    # Update mockup state if this is a mockup request
    mockup_state = await ReviewStorage.get_mockup_state(context, request_id)
    if mockup_state:
        mockup_state.current_menu = "options"
        await ReviewStorage.update_mockup_state(context, request_id, mockup_state)

    # Delegate to main handler
    await moderated_handlers._handle_accept_callback(query, context, callback_data)


async def _handle_reject_callback_with_mockup_state(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Handle reject button with mockup state management."""
    request_id = callback_data[len(CALLBACK_REJECT) :]

    # Update mockup state if this is a mockup request
    mockup_state = await ReviewStorage.get_mockup_state(context, request_id)
    if mockup_state:
        mockup_state.current_menu = "rejected"
        await ReviewStorage.update_mockup_state(context, request_id, mockup_state)

    # Delegate to main handler
    await moderated_handlers._handle_reject_callback(query, context, callback_data)


async def _handle_submit_callback_with_mockup_state(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Handle submit acceptance with mockup state management."""
    from rich.console import Console
    console = Console()

    request_id = callback_data[len(CALLBACK_SUBMIT_ACCEPTANCE) :]
    console.print(f"[magenta]=== ENHANCED SUBMIT CALLBACK for request {request_id} ===[/magenta]")

    # Check if this is a mockup request
    mockup_state = await ReviewStorage.get_mockup_state(context, request_id)
    console.print(f"[blue]Mockup state exists: {mockup_state is not None}[/blue]")

    # IMPORTANT: Only treat as mockup if it has BOTH mockup_state AND was created by /mockup command
    # Don't let auto-recovery create mockup state for real requests
    pending_review = await ReviewStorage.get_pending_review(context, request_id)
    is_real_mockup = mockup_state is not None and pending_review is not None and pending_review.original_chat_id == pending_review.review_chat_id
    console.print(f"[blue]Is real mockup (same chat): {is_real_mockup}[/blue]")

    if mockup_state and is_real_mockup:
        # Update mockup state to completed
        mockup_state.current_menu = "completed"
        await ReviewStorage.update_mockup_state(context, request_id, mockup_state)

        # For mockup requests, show a completion message instead of actually processing
        pending_review = await ReviewStorage.get_pending_review(context, request_id)
        if not pending_review:
            await query.edit_message_text(" Request not found or expired")
            return

        options_state = await ReviewStorage.get_options_state(context, request_id)

        # Show mockup completion message
        options_summary = []
        if options_state.alt:
            options_summary.append(" Alternative Dumper")
        if options_state.force:
            options_summary.append(" Force Re-Dump")
        if options_state.privdump:
            options_summary.append(" Private Dump")

        options_text = (
            "\n".join(options_summary) if options_summary else "No special options selected"
        )

        await query.edit_message_text(
            text=f" Request {request_id} accepted and dumpyara job triggered\n\nSelected options:\n{options_text}\n\nURL: {pending_review.url}"
        )
    else:
        # For real requests, delegate to main handler
        console.print("[cyan]Delegating to real moderated_handlers._handle_submit_callback[/cyan]")
        await moderated_handlers._handle_submit_callback(query, context, callback_data)


async def _handle_toggle_callback_with_mockup_state(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str, option: str
) -> None:
    """Handle option toggles with mockup state preservation."""
    # Delegate to main handler without any special mockup state handling
    # The main handler should not cause cleanup for mockup requests
    await moderated_handlers._handle_toggle_callback(query, context, callback_data, option)


async def _handle_cancel_callback_with_mockup_state(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Handle cancel request callback with mockup state management."""
    request_id = callback_data.replace(CALLBACK_CANCEL_REQUEST, "")

    # Check if this is a mockup request
    mockup_state = await ReviewStorage.get_mockup_state(context, request_id)
    if mockup_state:
        # Update mockup state to cancelled
        mockup_state.current_menu = "cancelled"
        await ReviewStorage.update_mockup_state(context, request_id, mockup_state)

        # For mockup, just show cancellation message
        await query.edit_message_text(
            text=f" Request {request_id} cancelled",
            reply_markup=None,
        )
    else:
        # For real requests, delegate to main handler
        await moderated_handlers._handle_cancel_callback(query, context, callback_data)


