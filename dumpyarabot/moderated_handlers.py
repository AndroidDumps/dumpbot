import re
from datetime import datetime, timezone
import secrets
from typing import Any, Optional

from rich.console import Console
from telegram import Chat, Message, ReplyParameters, Update
from telegram.ext import ContextTypes

from dumpyarabot import schemas, utils, url_utils
from dumpyarabot.utils import escape_markdown
from dumpyarabot.config import (CALLBACK_ACCEPT, CALLBACK_CANCEL_REQUEST,
                                CALLBACK_REJECT, CALLBACK_SUBMIT_ACCEPTANCE,
                                CALLBACK_TOGGLE_ALT, CALLBACK_TOGGLE_FORCE,
                                CALLBACK_TOGGLE_PRIVDUMP, settings)
from dumpyarabot.message_queue import message_queue
from dumpyarabot.storage import ReviewStorage
from dumpyarabot.ui import (ACCEPTANCE_TEMPLATE, REJECTION_TEMPLATE, REVIEW_TEMPLATE, SUBMISSION_TEMPLATE,
                            create_options_keyboard, create_review_keyboard)

console = Console()


def _truncate_message(text: str, max_length: int = 300) -> str:
    """Truncate a message to fit in the review template, preserving readability."""
    if len(text) <= max_length:
        return text

    # Try to truncate at a word boundary
    truncated = text[:max_length]
    last_space = truncated.rfind(' ')
    last_newline = truncated.rfind('\n')

    # Use the last word or line boundary, whichever is closer to the end
    boundary = max(last_space, last_newline)
    if boundary > max_length * 0.8:  # Only use boundary if it's not too early
        truncated = text[:boundary]

    return truncated + "..."



async def _cleanup_request(context: ContextTypes.DEFAULT_TYPE, request_id: str) -> None:
    """Clean up a processed request - remove from storage but keep submission message for status updates."""
    await ReviewStorage.remove_pending_review(context, request_id)
    await ReviewStorage.remove_options_state(context, request_id)


async def handle_request_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle #request messages with URL parsing and validation."""
    chat: Optional[Chat] = update.effective_chat
    message: Optional[Message] = update.effective_message
    user = update.effective_user

    if not chat or not message or not user:
        console.print("[red]Chat, message or user object is None[/red]")
        return

    # 1. Check if message is in REQUEST_CHAT_ID
    if chat.id != settings.REQUEST_CHAT_ID:
        console.print(f"[yellow]Message from non-request chat: {chat.id}[/yellow]")
        return

    # 2. Parse message for "#request <URL>" pattern (flexible format)
    # Supports: "#requesthttps://...", "#request https://...", "#request please https://...", etc.
    # DOTALL flag allows . to match newlines for multi-line messages
    request_pattern = r"#request\s*.*?(https?://[^\s]+)"
    match = re.search(request_pattern, message.text or "", re.IGNORECASE | re.DOTALL)

    if not match:
        console.print("[yellow]No valid #request pattern found[/yellow]")
        return

    url_str = match.group(1)
    console.print(f"[blue]Processing request for URL: {url_str}[/blue]")

    try:
        # 3. Validate URL using new utility
        is_valid, validated_url, error_msg = await url_utils.validate_and_normalize_url(url_str)
        if not is_valid:
            raise ValueError(error_msg)

        # 4. Generate request_id
        request_id = utils.generate_request_id()

        # 5. Send review message to REVIEW_CHAT_ID with Accept/Reject buttons
        raw_message = message.text or ""
        # Remove the URL from the original message since it's already displayed above
        message_without_url = re.sub(r'https?://[^\s]+', '', raw_message).strip()
        # Remove #request tag and extra whitespace
        message_without_url = re.sub(r'#request\s*', '', message_without_url).strip()
        original_message = _truncate_message(message_without_url) if message_without_url else "No additional text"
        review_text = REVIEW_TEMPLATE.format(
            username=user.username or user.first_name or str(user.id),
            url=validated_url,
            request_id=request_id,
            original_message=original_message,
        )

        # Send review message directly to get real Telegram message ID
        from telegram import InlineKeyboardMarkup
        review_keyboard = create_review_keyboard(request_id)
        review_message = await message_queue.send_immediate_message(
            chat_id=settings.REVIEW_CHAT_ID,
            text=review_text,
            parse_mode=settings.DEFAULT_PARSE_MODE,
            reply_to_message_id=None,
            disable_web_page_preview=True,
        )
        # Attach the keyboard by editing (send_immediate_message doesn't support keyboards)
        await context.bot.edit_message_reply_markup(
            chat_id=settings.REVIEW_CHAT_ID,
            message_id=review_message.message_id,
            reply_markup=review_keyboard,
        )

        # 6. Notify user of successful submission directly to get real Telegram message ID
        submission_message = await message_queue.send_immediate_message(
            chat_id=chat.id,
            text=SUBMISSION_TEMPLATE.format(url=validated_url),
            parse_mode=settings.DEFAULT_PARSE_MODE,
            reply_to_message_id=message.message_id,
            disable_web_page_preview=True,
        )

        # 7. Store PendingReview in bot_data (URL as string for Redis compatibility)
        pending_review = schemas.PendingReview(
            request_id=request_id,
            original_chat_id=chat.id,
            original_message_id=message.message_id,
            requester_id=user.id,
            requester_username=user.username,
            url=str(validated_url),  # Convert AnyHttpUrl to string for storage
            review_chat_id=settings.REVIEW_CHAT_ID,
            review_message_id=review_message.message_id,
            submission_confirmation_message_id=submission_message.message_id,
        )

        await ReviewStorage.store_pending_review(context, pending_review)

        console.print(f"[green]Request {request_id} processed successfully[/green]")

    except ValueError:
        console.print(f"[red]Invalid URL provided: {url_str}[/red]")
        await message_queue.send_error(
            chat_id=chat.id,
            text=" Invalid URL format provided",
            context={"moderated_request": True, "url": url_str, "error": "invalid_url"}
        )
    except Exception as e:
        console.print(f"[red]Error processing request: {e}[/red]")
        console.print_exception()
        await message_queue.send_error(
            chat_id=chat.id,
            text=" An error occurred while processing your request",
            context={"moderated_request": True, "url": url_str, "error": "processing_failed"}
        )


async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle button callbacks for accept/reject and option toggles."""
    console.print("[magenta]=== CALLBACK QUERY HANDLER STARTED ===[/magenta]")

    query = update.callback_query
    if not query or not query.data:
        console.print("[red]Query or query data is None[/red]")
        return

    await query.answer()

    callback_data = query.data
    console.print(f"[blue]Processing callback: {callback_data}[/blue]")

    # Parse callback_data to determine action type
    if callback_data.startswith(CALLBACK_ACCEPT):
        console.print("[cyan]Taking ACCEPT callback path[/cyan]")
        await _handle_accept_callback(query, context, callback_data)
    elif callback_data.startswith(CALLBACK_REJECT):
        console.print("[cyan]Taking REJECT callback path[/cyan]")
        await _handle_reject_callback(query, context, callback_data)
    elif callback_data.startswith(CALLBACK_TOGGLE_ALT):
        console.print("[cyan]Taking TOGGLE_ALT callback path[/cyan]")
        await _handle_toggle_callback(query, context, callback_data, "alt")
    elif callback_data.startswith(CALLBACK_TOGGLE_FORCE):
        console.print("[cyan]Taking TOGGLE_FORCE callback path[/cyan]")
        await _handle_toggle_callback(query, context, callback_data, "force")
    elif callback_data.startswith(CALLBACK_TOGGLE_PRIVDUMP):
        console.print("[cyan]Taking TOGGLE_PRIVDUMP callback path[/cyan]")
        await _handle_toggle_callback(query, context, callback_data, "privdump")
    elif callback_data.startswith(CALLBACK_CANCEL_REQUEST):
        console.print("[cyan]Taking CANCEL_REQUEST callback path[/cyan]")
        await _handle_cancel_callback(query, context, callback_data)
    elif callback_data.startswith(CALLBACK_SUBMIT_ACCEPTANCE):
        console.print("[cyan]Taking SUBMIT_ACCEPTANCE callback path[/cyan]")
        await _handle_submit_callback(query, context, callback_data)
    else:
        console.print(f"[red]Unknown callback data: {callback_data}[/red]")


async def _handle_accept_callback(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Handle accept button -> Show options submenu."""
    request_id = callback_data[len(CALLBACK_ACCEPT) :]

    pending_review = await ReviewStorage.get_pending_review(context, request_id)
    if not pending_review:
        await query.edit_message_text(" Request not found or expired")
        return

    # Get current options state
    options_state = await ReviewStorage.get_options_state(context, request_id)

    # Update message to show options
    await query.edit_message_text(
        text=f" Configure options for request {request_id}\nURL: {pending_review.url}",
        reply_markup=create_options_keyboard(request_id, options_state),
        disable_web_page_preview=True,
    )


async def _handle_reject_callback(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Handle reject button -> Prompt for /reject command."""
    request_id = callback_data[len(CALLBACK_REJECT) :]

    await query.edit_message_text(
        text=f" To test reject request {request_id}, use:\n/reject {request_id} [reason]\n\nOr reply to this message with:\n/reject [reason]",
    )


async def _handle_toggle_callback(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    callback_data: str,
    option: str,
) -> None:
    """Handle option toggles -> Update state and refresh keyboard."""
    # Extract request_id by stripping the known prefix
    prefix_map = {
        "alt": CALLBACK_TOGGLE_ALT,
        "force": CALLBACK_TOGGLE_FORCE,
        "privdump": CALLBACK_TOGGLE_PRIVDUMP,
    }
    prefix = prefix_map[option]
    request_id = callback_data[len(prefix):]

    pending_review = await ReviewStorage.get_pending_review(context, request_id)
    if not pending_review:
        await query.edit_message_text(" Request not found or expired")
        return

    # Update option state
    options_state = await ReviewStorage.get_options_state(context, request_id)

    if option == "alt":
        options_state.alt = not options_state.alt
    elif option == "force":
        options_state.force = not options_state.force
    elif option == "privdump":
        options_state.privdump = not options_state.privdump

    await ReviewStorage.update_options_state(context, request_id, options_state)

    # Refresh keyboard with updated state
    await query.edit_message_reply_markup(
        reply_markup=create_options_keyboard(request_id, options_state)
    )


async def _handle_submit_callback(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Handle submit acceptance -> Process with selected options."""
    request_id = callback_data[len(CALLBACK_SUBMIT_ACCEPTANCE) :]
    console.print(f"[magenta]=== SUBMIT CALLBACK STARTED for request {request_id} ===[/magenta]")

    pending_review = await ReviewStorage.get_pending_review(context, request_id)
    if not pending_review:
        await query.edit_message_text(" Request not found or expired")
        return

    options_state = await ReviewStorage.get_options_state(context, request_id)

    try:
        # Create DumpArguments with the selected options
        dump_args = schemas.DumpArguments(
            url=schemas.AnyHttpUrl(pending_review.url),  # Convert string back to AnyHttpUrl
            use_alt_dumper=options_state.alt,
            use_privdump=options_state.privdump,
            initial_message_id=pending_review.original_message_id,
            initial_chat_id=pending_review.original_chat_id,
        )

        # Create dump job with metadata
        job = schemas.DumpJob(
            job_id=secrets.token_hex(8),
            dump_args=dump_args,
            created_at=datetime.now(timezone.utc),
            initial_message_id=pending_review.original_message_id,
            initial_chat_id=pending_review.original_chat_id
        )

        # Create enhanced job data with metadata structure
        enhanced_job_data = job.model_dump()
        enhanced_job_data["metadata"] = {
            "telegram_context": {
                "chat_id": pending_review.original_chat_id,
                "message_id": pending_review.original_message_id,
                "user_id": pending_review.requester_id,
                "url": pending_review.url
            }
        }

        console.print(f"[blue]Queueing dump job {job.job_id} with metadata...[/blue]")
        job_id = await message_queue.queue_dump_job_with_metadata(enhanced_job_data)
        console.print(f"[green]Successfully queued dump job {job_id} with metadata[/green]")

        # Notify original requester with acceptance message
        if options_state.privdump:
            user_message = (
                "Your request is under further review for private processing."
            )
        else:
            user_message = ACCEPTANCE_TEMPLATE

        console.print(f"[green]Sending acceptance message to user: {user_message}[/green]")
        console.print(f"[blue]Chat ID: {pending_review.original_chat_id}, Message ID: {pending_review.original_message_id}[/blue]")

        await message_queue.send_cross_chat(
            chat_id=pending_review.original_chat_id,
            text=user_message,
            reply_to_message_id=pending_review.original_message_id,
            reply_to_chat_id=pending_review.original_chat_id,
            context={"moderated_request": True, "request_id": request_id, "stage": "acceptance"}
        )

        console.print("[green]Acceptance message sent successfully[/green]")

        # Delete the admin confirmation message after successful job start
        await query.delete_message()
        await _cleanup_request(context, request_id)

    except Exception as e:
        console.print(f"[red]Error processing acceptance: {e}[/red]")
        console.print_exception()
        await query.edit_message_text(
            f" Error processing request {request_id}: {str(e)}"
        )


async def accept_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /accept command with request_id and option flags."""
    console.print("[magenta]=== ACCEPT COMMAND STARTED ===[/magenta]")

    chat: Optional[Chat] = update.effective_chat
    message: Optional[Message] = update.effective_message

    if not chat or not message:
        console.print("[red]Chat or message object is None[/red]")
        return

    # Ensure it can only be used in the correct review chat
    if chat.id != settings.REVIEW_CHAT_ID:
        console.print(f"[yellow]/accept used in wrong chat: {chat.id}[/yellow]")
        await message_queue.send_error(
            chat_id=chat.id,
            text="This command can only be used in the review chat",
            context={"command": "accept", "error": "wrong_chat", "chat_id": chat.id}
        )
        return

    # Try to extract request_id from reply or arguments
    request_id = None
    options = ""
    # Check if this is a reply to a bot message containing a request ID
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.is_bot:
        # Extract request_id from the replied message text
        replied_text = message.reply_to_message.text or ""
        # Look for request ID pattern in the replied message
        request_id_match = re.search(r"Request ID: ([a-f0-9]{8})", replied_text, re.IGNORECASE)
        if request_id_match:
            request_id = request_id_match.group(1)
            # All arguments become the options when using reply mode
            options = "".join(context.args) if context.args else ""
            console.print(f"[blue]Extracted request_id {request_id} from reply[/blue]")
        else:
            await message_queue.send_error(
                chat_id=chat.id,
                text=" Could not find a request ID in the replied message",
                context={"command": "accept", "error": "no_request_id_in_reply"}
            )
            return

    # Fallback to traditional argument parsing if not in reply mode
    elif not request_id:
        if not context.args:
            await message_queue.send_reply(
                chat_id=chat.id,
                text="Usage: `/accept \\[request\\_id\\] \\[options\\]` or reply to a review message with `/accept \\[options\\]`\nOptions: a\\=alt, f\\=force, p\\=privdump",
                reply_to_message_id=message.message_id,
                context={"command": "accept", "error": "missing_args"}
            )
            return

        request_id = context.args[0]
        options = "".join(context.args[1:]) if len(context.args) > 1 else ""

    # Validate request_id exists in pending reviews
    pending_review = await ReviewStorage.get_pending_review(context, request_id)
    if not pending_review:
        await message_queue.send_error(
            chat_id=chat.id,
            text=f" Request {request_id} not found or expired",
            context={"command": "accept", "error": "request_not_found", "request_id": request_id}
        )
        return

    # Parse option flags
    use_alt = "a" in options
    force = "f" in options
    use_privdump = "p" in options

    try:
        # Start dump process with options
        dump_args = schemas.DumpArguments(
            url=schemas.AnyHttpUrl(pending_review.url),  # Convert string back to AnyHttpUrl
            use_alt_dumper=use_alt,
            use_privdump=use_privdump,
            initial_message_id=pending_review.original_message_id,
            initial_chat_id=pending_review.original_chat_id,
        )

        # Create dump job with metadata
        job = schemas.DumpJob(
            job_id=secrets.token_hex(8),
            dump_args=dump_args,
            created_at=datetime.now(timezone.utc),
            initial_message_id=pending_review.original_message_id,
            initial_chat_id=pending_review.original_chat_id
        )

        # Create enhanced job data with metadata structure
        enhanced_job_data = job.model_dump()
        enhanced_job_data["metadata"] = {
            "telegram_context": {
                "chat_id": pending_review.original_chat_id,
                "message_id": pending_review.original_message_id,
                "user_id": pending_review.requester_id,
                "url": pending_review.url
            }
        }

        console.print(f"[blue]Queueing dump job {job.job_id} with metadata...[/blue]")
        job_id = await message_queue.queue_dump_job_with_metadata(enhanced_job_data)
        console.print(f"[green]Successfully queued dump job {job_id} with metadata[/green]")
        response_text = f"job queued with ID {job_id}"

        await message_queue.send_reply(
            chat_id=chat.id,
            text=f" Request {request_id} accepted and {response_text}",
            reply_to_message_id=message.message_id,
            context={"command": "accept", "action": "arq_queued", "request_id": request_id}
        )

        # Notify original requester with acceptance message
        if use_privdump:
            user_message = (
                "Your request is under further review for private processing."
            )
        else:
            user_message = ACCEPTANCE_TEMPLATE

        console.print(f"[green]Sending acceptance message via command to user: {user_message}[/green]")
        console.print(f"[blue]Chat ID: {pending_review.original_chat_id}, Message ID: {pending_review.original_message_id}[/blue]")

        await message_queue.send_cross_chat(
            chat_id=pending_review.original_chat_id,
            text=user_message,
            reply_to_message_id=pending_review.original_message_id,
            reply_to_chat_id=pending_review.original_chat_id,
            context={"command": "accept", "action": "acceptance_notification", "request_id": request_id}
        )

        console.print("[green]Acceptance message via command sent successfully[/green]")
        await _cleanup_request(context, request_id)

    except Exception as e:
        console.print(f"[red]Error processing acceptance: {e}[/red]")
        console.print_exception()
        await message_queue.send_error(
            chat_id=chat.id,
            text=f" Error processing request {request_id}: {str(e)}",
            context={"command": "accept", "error": "processing_exception", "request_id": request_id, "exception": str(e)}
        )


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reject command with request_id and reason."""
    chat: Optional[Chat] = update.effective_chat
    message: Optional[Message] = update.effective_message

    if not chat or not message:
        console.print("[red]Chat or message object is None[/red]")
        return

    # Ensure it can only be used in the correct review chat
    if chat.id != settings.REVIEW_CHAT_ID:
        console.print(f"[yellow]/reject used in wrong chat: {chat.id}[/yellow]")
        await message_queue.send_error(
            chat_id=chat.id,
            text="This command can only be used in the review chat",
            context={"command": "reject", "error": "wrong_chat", "chat_id": chat.id}
        )
        return

    # Try to extract request_id from reply or arguments
    request_id = None
    reason = "No reason provided"
    # Check if this is a reply to a bot message containing a request ID
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.is_bot:
        # Extract request_id from the replied message text
        replied_text = message.reply_to_message.text or ""
        # Look for request ID pattern in the replied message
        request_id_match = re.search(r"Request ID: ([a-f0-9]{8})", replied_text, re.IGNORECASE)
        if request_id_match:
            request_id = request_id_match.group(1)
            # All arguments become the reason when using reply mode
            reason = " ".join(context.args) if context.args else "No reason provided"
            console.print(f"[blue]Extracted request_id {request_id} from reply[/blue]")
        else:
            await message_queue.send_error(
                chat_id=chat.id,
                text=" Could not find a request ID in the replied message",
                context={"command": "reject", "error": "no_request_id_in_reply"}
            )
            return

    # Fallback to traditional argument parsing if not in reply mode
    elif not request_id:
        if not context.args:
            await message_queue.send_reply(
                chat_id=chat.id,
                text="Usage: `/reject \\[request\\_id\\] \\[reason\\]` or reply to a review message with `/reject \\[reason\\]`",
                reply_to_message_id=message.message_id,
                context={"command": "reject", "error": "missing_args"}
            )
            return

        request_id = context.args[0]
        reason = (
            " ".join(context.args[1:]) if len(context.args) > 1 else "No reason provided"
        )

    # Validate request_id exists
    pending_review = await ReviewStorage.get_pending_review(context, request_id)
    if not pending_review:
        await message_queue.send_error(
            chat_id=chat.id,
            text=f" Request {request_id} not found or expired",
            context={"command": "reject", "error": "request_not_found", "request_id": request_id}
        )
        return

    try:
        # Get admin info
        admin_user = update.effective_user
        admin_name = admin_user.username or admin_user.first_name or str(admin_user.id) if admin_user else "Unknown"


        # Delete the review message if this was a reply to it
        if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.is_bot:
            try:
                await context.bot.delete_message(
                    chat_id=chat.id,
                    message_id=message.reply_to_message.message_id
                )
            except Exception as e:
                console.print(f"[yellow]Could not delete review message: {e}[/yellow]")

        # Delete the reject command message
        try:
            await context.bot.delete_message(
                chat_id=chat.id,
                message_id=message.message_id
            )
        except Exception as e:
            console.print(f"[yellow]Could not delete command message: {e}[/yellow]")

        # Send cleaner final message in review chat with link to original request
        await message_queue.send_cross_chat(
            chat_id=chat.id,
            text=f" Request {request_id} rejected by @{admin_name}\nReason: {reason}",
            reply_to_message_id=pending_review.original_message_id,
            reply_to_chat_id=pending_review.original_chat_id,
            context={"command": "reject", "action": "rejection_confirmation", "request_id": request_id, "admin": admin_name}
        )

        # Log rejection with reason
        console.print(f"[yellow]Request {request_id} rejected by @{admin_name}: {reason}[/yellow]")

        # Notify original requester with rejection message
        await message_queue.send_cross_chat(
            chat_id=pending_review.original_chat_id,
            text=REJECTION_TEMPLATE.format(reason=reason),
            reply_to_message_id=pending_review.original_message_id,
            reply_to_chat_id=pending_review.original_chat_id,
            context={"command": "reject", "action": "user_notification", "request_id": request_id}
        )
        await _cleanup_request(context, request_id)

    except Exception as e:
        console.print(f"[red]Error processing rejection: {e}[/red]")
        console.print_exception()
        # Don't try to reply to the message since it might be deleted
        await message_queue.send_error(
            chat_id=chat.id,
            text=f" Error processing rejection for request {request_id}: {str(e)}",
            context={"command": "reject", "error": "processing_exception", "request_id": request_id, "exception": str(e)}
        )


async def _handle_cancel_callback(
    query: Any, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Handle cancel request callback."""
    request_id = callback_data.replace(CALLBACK_CANCEL_REQUEST, "")

    if not query.message:
        return

    pending = await ReviewStorage.get_pending_review(context, request_id)

    if not pending:
        await query.edit_message_text(
            text=" Request not found or already processed", reply_markup=None
        )
        return

    try:
        # Send cancellation message in review chat
        await message_queue.send_notification(
            chat_id=pending.review_chat_id,
            text=f" Request {request_id} cancelled by user @{pending.requester_username}",
            context={"action": "request_cancelled", "request_id": request_id, "user": pending.requester_username}
        )

        # Update submission confirmation message to show cancelled
        await query.edit_message_text(text=" Request cancelled", reply_markup=None)

        # Clean up request data
        await _cleanup_request(context, request_id)

        console.print(f"[yellow]Request {request_id} cancelled by user[/yellow]")

    except Exception as e:
        console.print(f"[red]Error cancelling request: {e}[/red]")
        console.print_exception()
        await query.edit_message_text(
            text=" Error cancelling request", reply_markup=None
        )
