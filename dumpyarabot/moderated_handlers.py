import re
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from rich.console import Console
from telegram import Chat, Message, ReplyParameters, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from dumpyarabot import schemas, url_utils, utils
from dumpyarabot.config import (
    CALLBACK_ACCEPT,
    CALLBACK_CANCEL_REQUEST,
    CALLBACK_REJECT,
    CALLBACK_SUBMIT_ACCEPTANCE,
    CALLBACK_TOGGLE_ALT,
    CALLBACK_TOGGLE_FORCE,
    CALLBACK_TOGGLE_PRIVDUMP,
    settings,
)
from dumpyarabot.message_formatting import format_firmware_inputs, generate_progress_bar
from dumpyarabot.message_queue import message_queue
from dumpyarabot.privacy import (
    PRIVATE_URL_PLACEHOLDER,
    redact_for_job,
    redact_urls,
    sanitize_url,
)
from dumpyarabot.storage import ReviewStorage
from dumpyarabot.ui import (
    ACCEPTANCE_TEMPLATE,
    REJECTION_TEMPLATE,
    REVIEW_TEMPLATE,
    SUBMISSION_TEMPLATE,
    create_options_keyboard,
    create_review_keyboard,
)
from dumpyarabot.utils import escape_markdown

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


def _pending_urls(pending_review: schemas.PendingReview) -> list[str]:
    return [pending_review.url, *pending_review.delta_urls]


def _moderation_url_summary(urls: list[str], *, private: bool) -> str:
    if private:
        return PRIVATE_URL_PLACEHOLDER
    return "\n".join(
        f"{index}. {escape_markdown(sanitize_url(url))}"
        for index, url in enumerate(urls, start=1)
    )


def _options_message_text(
    request_id: str,
    pending_review: schemas.PendingReview,
    *,
    private: bool,
) -> str:
    base_url = PRIVATE_URL_PLACEHOLDER if private else sanitize_url(pending_review.url)
    return (
        f" Configure options for request {request_id}\n"
        f"URL: {base_url}\n"
        f"Delta OTAs: {len(pending_review.delta_urls)}"
    )


async def _edit_message_text_if_changed(bot: Any, **kwargs: Any) -> None:
    try:
        await bot.edit_message_text(**kwargs)
    except BadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


async def _delete_message_if_present(bot: Any, **kwargs: Any) -> None:
    try:
        await bot.delete_message(**kwargs)
    except BadRequest as error:
        error_lower = str(error).lower()
        if (
            "message to delete not found" not in error_lower
            and "message can't be deleted" not in error_lower
        ):
            raise


async def _sync_bot_owned_request_summaries(
    context: ContextTypes.DEFAULT_TYPE,
    request_id: str,
    pending_review: schemas.PendingReview,
    options_state: schemas.AcceptOptionsState,
) -> None:
    """Apply the selected privacy state to both bot-owned request summaries."""
    await _edit_message_text_if_changed(
        bot=context.bot,
        chat_id=pending_review.review_chat_id,
        message_id=pending_review.review_message_id,
        text=_options_message_text(
            request_id,
            pending_review,
            private=options_state.privdump,
        ),
        reply_markup=create_options_keyboard(request_id, options_state),
        disable_web_page_preview=True,
    )

    if pending_review.submission_confirmation_message_id is not None:
        summary = _moderation_url_summary(
            _pending_urls(pending_review),
            private=options_state.privdump,
        )
        try:
            await _edit_message_text_if_changed(
                bot=context.bot,
                chat_id=pending_review.original_chat_id,
                message_id=pending_review.submission_confirmation_message_id,
                text=SUBMISSION_TEMPLATE.format(url=summary),
                parse_mode=settings.DEFAULT_PARSE_MODE,
                disable_web_page_preview=True,
            )
        except BadRequest as error:
            if "message to edit not found" in str(error).lower():
                pending_review.submission_confirmation_message_id = None
                await ReviewStorage.update_pending_review(context, pending_review)
            else:
                raise

    if options_state.privdump:
        await _detach_private_submission_confirmation(context, pending_review)


async def _detach_private_submission_confirmation(
    context: ContextTypes.DEFAULT_TYPE,
    pending_review: schemas.PendingReview,
) -> None:
    """Replace a reply confirmation so Telegram cannot quote the source message."""
    stale_message_id = pending_review.stale_submission_confirmation_message_id
    if stale_message_id is not None:
        await _delete_message_if_present(
            context.bot,
            chat_id=pending_review.original_chat_id,
            message_id=stale_message_id,
        )
        pending_review.stale_submission_confirmation_message_id = None
        await ReviewStorage.update_pending_review(context, pending_review)

    message_id = pending_review.submission_confirmation_message_id
    if message_id is None or not pending_review.submission_replies_to_request:
        return

    replacement = await context.bot.send_message(
        chat_id=pending_review.original_chat_id,
        text=SUBMISSION_TEMPLATE.format(url=PRIVATE_URL_PLACEHOLDER),
        parse_mode=settings.DEFAULT_PARSE_MODE,
        disable_web_page_preview=True,
    )
    pending_review.submission_confirmation_message_id = replacement.message_id
    pending_review.submission_replies_to_request = False
    pending_review.stale_submission_confirmation_message_id = message_id
    await ReviewStorage.update_pending_review(context, pending_review)
    await _delete_message_if_present(
        context.bot,
        chat_id=pending_review.original_chat_id,
        message_id=message_id,
    )
    pending_review.stale_submission_confirmation_message_id = None
    await ReviewStorage.update_pending_review(context, pending_review)


async def _delete_original_private_request(
    context: ContextTypes.DEFAULT_TYPE,
    pending_review: schemas.PendingReview,
) -> None:
    """Best-effort removal of the requester's URL-bearing message."""
    pending_review.original_message_private = True
    try:
        await ReviewStorage.update_pending_review(context, pending_review)
    except Exception:
        console.print("[yellow]Could not persist private request state[/yellow]")
    await _delete_private_request_message(
        context,
        pending_review.original_chat_id,
        pending_review.original_message_id,
    )


async def _delete_private_request_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        console.print("[yellow]Could not delete the original private request message[/yellow]")


async def _prepare_private_acceptance(
    context: ContextTypes.DEFAULT_TYPE,
    request_id: str,
    pending_review: schemas.PendingReview,
    options_state: schemas.AcceptOptionsState,
) -> None:
    await _delete_original_private_request(context, pending_review)
    await _sync_bot_owned_request_summaries(
        context,
        request_id,
        pending_review,
        options_state,
    )


async def _create_status_message(
    context: ContextTypes.DEFAULT_TYPE,
    pending_review: schemas.PendingReview,
    dump_args: schemas.DumpArguments,
    job_id: str,
) -> tuple[int, int, str]:
    """Create the bot-owned status message that later worker updates will edit."""
    primary_allowed_chat = settings.ALLOWED_CHATS[0] if settings.ALLOWED_CHATS else pending_review.review_chat_id
    initial_text = _build_status_message_text(dump_args, job_id)

    reply_parameters = None
    if not dump_args.use_privdump and not pending_review.original_message_private:
        reply_parameters = ReplyParameters(
            message_id=pending_review.original_message_id,
            chat_id=pending_review.original_chat_id,
        )
    status_message = await context.bot.send_message(
        chat_id=primary_allowed_chat,
        text=initial_text,
        parse_mode=settings.DEFAULT_PARSE_MODE,
        disable_web_page_preview=True,
        reply_parameters=reply_parameters,
    )
    return status_message.message_id, primary_allowed_chat, initial_text


def _build_status_message_text(dump_args: schemas.DumpArguments, job_id: str) -> str:
    """Build the initial worker status message text."""
    if dump_args.use_privdump:
        initial_text = " *Private Dump Job Queued*\n\n"
    else:
        initial_text = " *Firmware Dump Queued*\n\n"
        initial_text += format_firmware_inputs(dump_args.model_dump())

    initial_text += f"*Job ID:* `{job_id}`\n"

    options_list = []
    if dump_args.use_alt_dumper:
        options_list.append("Alt Dumper")
    if dump_args.force:
        options_list.append("Force")
    if dump_args.use_privdump:
        options_list.append("Private")
    if options_list:
        initial_text += f" *Options:* {', '.join(options_list)}\n"

    initial_text += f"\n{generate_progress_bar(None)}\n"
    initial_text += " Queued for processing...\n\n"
    initial_text += "*Elapsed:* 0s\n"
    initial_text += " *Worker:* Waiting for assignment...\n"
    return initial_text


async def handle_request_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle legacy #request messages with URL parsing and validation."""
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

    # 2. Capture every ordered URL following the #request tag.
    raw_message = message.text or ""
    tag_match = re.search(r"#request", raw_message, re.IGNORECASE)
    url_strings = (
        re.findall(r"https?://[^\s]+", raw_message[tag_match.end() :], re.IGNORECASE)
        if tag_match
        else []
    )

    if not url_strings:
        console.print("[yellow]No valid #request pattern found[/yellow]")
        return

    message_without_url = re.sub(r'https?://[^\s]+', '', raw_message).strip()
    message_without_url = re.sub(r'#request\s*', '', message_without_url).strip()
    original_message = _truncate_message(message_without_url) if message_without_url else "No additional text"
    await _create_moderated_request(
        update,
        context,
        url_strings,
        schemas.AcceptOptionsState(),
        original_message,
    )


async def handle_moderated_dump(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Create a moderated request from /dump arguments."""
    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message:
        return

    try:
        url_strings, options = url_utils.parse_dump_tokens(list(context.args or []))
    except ValueError as error:
        await message_queue.send_reply(
            chat_id=chat.id,
            text=str(error),
            reply_to_message_id=message.message_id,
            context={"command": "dump", "error": "missing_urls"},
        )
        return

    options_state = schemas.AcceptOptionsState(
        alt="a" in options,
        force="f" in options,
        privdump="p" in options,
    )
    if options_state.privdump:
        await _delete_private_request_message(context, chat.id, message.message_id)
    await _create_moderated_request(
        update,
        context,
        url_strings,
        options_state,
        "No additional text",
    )


async def _create_moderated_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url_strings: list[str],
    options_state: schemas.AcceptOptionsState,
    original_message: str,
) -> None:
    """Validate, display, and persist one moderated request."""
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user
    if not chat or not message or not user:
        return

    console.print(f"[blue]Processing request with {len(url_strings)} firmware URL(s)[/blue]")

    try:
        # Validate every URL without changing its order.
        validated_urls = []
        for url_str in url_strings:
            is_valid, validated_url, error_msg = await url_utils.validate_and_normalize_url(url_str)
            if not is_valid or validated_url is None:
                raise ValueError(error_msg)
            validated_urls.append(validated_url)

        request_id = utils.generate_request_id()

        url_summary = _moderation_url_summary(
            validated_urls,
            private=options_state.privdump,
        )
        review_text = REVIEW_TEMPLATE.format(
            username=escape_markdown(user.username or user.first_name or str(user.id)),
            url=url_summary,
            request_id=request_id,
            original_message=escape_markdown(original_message),
        )

        review_keyboard = create_review_keyboard(request_id)
        review_message = await message_queue.send_immediate_message(
            chat_id=settings.REVIEW_CHAT_ID,
            text=review_text,
            parse_mode=settings.DEFAULT_PARSE_MODE,
            reply_to_message_id=None,
            disable_web_page_preview=True,
        )
        await context.bot.edit_message_reply_markup(
            chat_id=settings.REVIEW_CHAT_ID,
            message_id=review_message.message_id,
            reply_markup=review_keyboard,
        )

        submission_message = await message_queue.send_immediate_message(
            chat_id=chat.id,
            text=SUBMISSION_TEMPLATE.format(url=url_summary),
            parse_mode=settings.DEFAULT_PARSE_MODE,
            reply_to_message_id=(
                None if options_state.privdump else message.message_id
            ),
            disable_web_page_preview=True,
        )

        pending_review = schemas.PendingReview(
            request_id=request_id,
            original_chat_id=chat.id,
            original_message_id=message.message_id,
            requester_id=user.id,
            requester_username=user.username,
            url=validated_urls[0],
            delta_urls=validated_urls[1:],
            review_chat_id=settings.REVIEW_CHAT_ID,
            review_message_id=review_message.message_id,
            submission_confirmation_message_id=submission_message.message_id,
            submission_replies_to_request=not options_state.privdump,
            original_message_private=options_state.privdump,
        )

        await ReviewStorage.store_pending_review_with_options(
            context,
            pending_review,
            options_state,
        )

        console.print(f"[green]Request {request_id} processed successfully[/green]")

    except ValueError:
        console.print("[red]Invalid URL provided in moderated request[/red]")
        await message_queue.send_error(
            chat_id=chat.id,
            text=" Invalid URL format provided",
            context={"moderated_request": True, "error": "invalid_url"},
        )
    except Exception as error:
        safe_error = redact_urls(error, private=options_state.privdump)
        console.print(f"[red]Error processing request: {safe_error}[/red]")
        if not options_state.privdump:
            console.print_exception()
        await message_queue.send_error(
            chat_id=chat.id,
            text=" An error occurred while processing your request",
            context={"moderated_request": True, "error": "processing_failed"},
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
        text=_options_message_text(
            request_id,
            pending_review,
            private=options_state.privdump,
        ),
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

    if option == "privdump" and options_state.privdump:
        await _delete_original_private_request(context, pending_review)

    await ReviewStorage.update_options_state(context, request_id, options_state)

    try:
        await _sync_bot_owned_request_summaries(
            context,
            request_id,
            pending_review,
            options_state,
        )
    except Exception:
        console.print("[yellow]Could not update moderated request privacy[/yellow]")
        await query.edit_message_text(" Could not update request options")
        return


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
        if options_state.privdump:
            await _prepare_private_acceptance(
                context,
                request_id,
                pending_review,
                options_state,
            )

        # Create DumpArguments with the selected options
        dump_args = schemas.DumpArguments(
            url=schemas.AnyHttpUrl(pending_review.url),  # Convert string back to AnyHttpUrl
            delta_urls=pending_review.delta_urls,
            use_alt_dumper=options_state.alt,
            force=options_state.force,
            use_privdump=options_state.privdump,
            initial_message_id=(
                None
                if options_state.privdump or pending_review.original_message_private
                else pending_review.original_message_id
            ),
            initial_chat_id=pending_review.original_chat_id,
        )

        job_id = secrets.token_hex(8)
        status_message_id, status_chat_id, queued_text = await _create_status_message(
            context,
            pending_review,
            dump_args,
            job_id,
        )

        # Create dump job with metadata
        job = schemas.DumpJob(
            job_id=job_id,
            dump_args=dump_args,
            created_at=datetime.now(timezone.utc),
            initial_message_id=status_message_id,
            initial_chat_id=status_chat_id
        )

        # Create enhanced job data with metadata structure
        enhanced_job_data = job.model_dump()
        enhanced_job_data["_queued_text"] = queued_text
        telegram_context = {
                "chat_id": pending_review.original_chat_id,
                "message_id": pending_review.original_message_id,
                "user_id": pending_review.requester_id,
                "moderated_request": True,
        }
        enhanced_job_data["metadata"] = {"telegram_context": telegram_context}

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

        notification_context = {
            "moderated_request": True,
            "request_id": request_id,
            "stage": "acceptance",
        }
        if options_state.privdump:
            await message_queue.send_reply(
                chat_id=pending_review.original_chat_id,
                text=user_message,
                context=notification_context,
            )
        elif not pending_review.original_message_private:
            await message_queue.send_cross_chat(
                chat_id=pending_review.original_chat_id,
                text=user_message,
                reply_to_message_id=pending_review.original_message_id,
                reply_to_chat_id=pending_review.original_chat_id,
                context=notification_context,
            )
        else:
            await message_queue.send_reply(
                chat_id=pending_review.original_chat_id,
                text=user_message,
                context=notification_context,
            )

        console.print("[green]Acceptance message sent successfully[/green]")

        # Delete the admin confirmation message after successful job start
        try:
            await query.delete_message()
        except Exception as e:
            console.print(f"[yellow]Could not delete review message: {e}[/yellow]")
        await _cleanup_request(context, request_id)

    except Exception as e:
        safe_error = (
            redact_for_job(e, dump_args)
            if options_state.privdump and "dump_args" in locals()
            else redact_urls(e, private=options_state.privdump)
        )
        console.print(f"[red]Error processing acceptance: {safe_error}[/red]")
        if not options_state.privdump:
            console.print_exception()
        await query.edit_message_text(
            f" Error processing request {request_id}: {safe_error}"
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
                text="Usage: `/accept [request_id] [options]` or reply to a review message with `/accept [options]`\nOptions: a=alt, f=force, p=privdump",
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

    # Add explicit moderator flags to the requester's persisted defaults.
    options_state = await ReviewStorage.get_options_state(context, request_id)
    if options:
        options_state.alt = options_state.alt or "a" in options
        options_state.force = options_state.force or "f" in options
        options_state.privdump = options_state.privdump or "p" in options
        await ReviewStorage.update_options_state(context, request_id, options_state)

    use_alt = options_state.alt
    force = options_state.force
    use_privdump = options_state.privdump

    try:
        if use_privdump:
            await _prepare_private_acceptance(
                context,
                request_id,
                pending_review,
                options_state,
            )

        # Start dump process with options
        dump_args = schemas.DumpArguments(
            url=schemas.AnyHttpUrl(pending_review.url),  # Convert string back to AnyHttpUrl
            delta_urls=pending_review.delta_urls,
            use_alt_dumper=use_alt,
            force=force,
            use_privdump=use_privdump,
            initial_message_id=(
                None
                if use_privdump or pending_review.original_message_private
                else pending_review.original_message_id
            ),
            initial_chat_id=pending_review.original_chat_id,
        )

        job_id = secrets.token_hex(8)
        status_message_id, status_chat_id, queued_text = await _create_status_message(
            context,
            pending_review,
            dump_args,
            job_id,
        )

        # Create dump job with metadata
        job = schemas.DumpJob(
            job_id=job_id,
            dump_args=dump_args,
            created_at=datetime.now(timezone.utc),
            initial_message_id=status_message_id,
            initial_chat_id=status_chat_id
        )

        # Create enhanced job data with metadata structure
        enhanced_job_data = job.model_dump()
        enhanced_job_data["_queued_text"] = queued_text
        telegram_context = {
                "chat_id": pending_review.original_chat_id,
                "message_id": pending_review.original_message_id,
                "user_id": pending_review.requester_id,
                "moderated_request": True,
        }
        enhanced_job_data["metadata"] = {"telegram_context": telegram_context}

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

        notification_context = {
            "command": "accept",
            "action": "acceptance_notification",
            "request_id": request_id,
        }
        if use_privdump:
            await message_queue.send_reply(
                chat_id=pending_review.original_chat_id,
                text=user_message,
                context=notification_context,
            )
        elif not pending_review.original_message_private:
            await message_queue.send_cross_chat(
                chat_id=pending_review.original_chat_id,
                text=user_message,
                reply_to_message_id=pending_review.original_message_id,
                reply_to_chat_id=pending_review.original_chat_id,
                context=notification_context,
            )
        else:
            await message_queue.send_reply(
                chat_id=pending_review.original_chat_id,
                text=user_message,
                context=notification_context,
            )

        console.print("[green]Acceptance message via command sent successfully[/green]")
        await _cleanup_request(context, request_id)

    except Exception as e:
        safe_error = (
            redact_for_job(e, dump_args)
            if use_privdump and "dump_args" in locals()
            else redact_urls(e, private=use_privdump)
        )
        console.print(f"[red]Error processing acceptance: {safe_error}[/red]")
        if not use_privdump:
            console.print_exception()
        await message_queue.send_error(
            chat_id=chat.id,
            text=f" Error processing request {request_id}: {safe_error}",
            context={"command": "accept", "error": "processing_exception", "request_id": request_id}
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
                text="Usage: `/reject [request_id] [reason]` or reply to a review message with `/reject [reason]`",
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

    options_state = await ReviewStorage.get_options_state(context, request_id)
    private_request = (
        options_state.privdump or pending_review.original_message_private
    )

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

        rejection_confirmation = f" Request {request_id} rejected by @{admin_name}\nReason: {reason}"
        rejection_context = {
            "command": "reject",
            "action": "rejection_confirmation",
            "request_id": request_id,
            "admin": admin_name,
        }
        if private_request:
            await message_queue.send_reply(
                chat_id=chat.id,
                text=rejection_confirmation,
                context=rejection_context,
            )
        else:
            await message_queue.send_cross_chat(
                chat_id=chat.id,
                text=rejection_confirmation,
                reply_to_message_id=pending_review.original_message_id,
                reply_to_chat_id=pending_review.original_chat_id,
                context=rejection_context,
            )

        # Log rejection with reason
        console.print(f"[yellow]Request {request_id} rejected by @{admin_name}: {reason}[/yellow]")

        # Notify original requester with rejection message
        user_rejection = REJECTION_TEMPLATE.format(reason=reason)
        user_context = {
            "command": "reject",
            "action": "user_notification",
            "request_id": request_id,
        }
        if private_request:
            await message_queue.send_reply(
                chat_id=pending_review.original_chat_id,
                text=user_rejection,
                context=user_context,
            )
        else:
            await message_queue.send_cross_chat(
                chat_id=pending_review.original_chat_id,
                text=user_rejection,
                reply_to_message_id=pending_review.original_message_id,
                reply_to_chat_id=pending_review.original_chat_id,
                context=user_context,
            )
        await _cleanup_request(context, request_id)

    except Exception as e:
        safe_error = redact_urls(e, private=private_request)
        console.print(f"[red]Error processing rejection: {safe_error}[/red]")
        if not private_request:
            console.print_exception()
        # Don't try to reply to the message since it might be deleted
        await message_queue.send_error(
            chat_id=chat.id,
            text=f" Error processing rejection for request {request_id}: {safe_error}",
            context={"command": "reject", "error": "rejection_exception", "request_id": request_id},
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
        user_display = (
            f"@{pending.requester_username}"
            if pending.requester_username
            else f"User {pending.requester_id}"
        )
        # Send cancellation message in review chat
        await message_queue.send_notification(
            chat_id=pending.review_chat_id,
            text=f" Request {request_id} cancelled by user {user_display}",
            context={"action": "request_cancelled", "request_id": request_id, "user": pending.requester_username}
        )

        # Update submission confirmation message to show cancelled
        await query.edit_message_text(text=" Request cancelled", reply_markup=None)

        # Clean up request data
        await _cleanup_request(context, request_id)

        console.print(f"[yellow]Request {request_id} cancelled by user[/yellow]")

    except Exception as e:
        safe_error = redact_urls(e, private=pending.original_message_private)
        console.print(f"[red]Error cancelling request: {safe_error}[/red]")
        if not pending.original_message_private:
            console.print_exception()
        await query.edit_message_text(
            text=" Error cancelling request", reply_markup=None
        )
