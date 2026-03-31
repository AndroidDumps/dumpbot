from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from dumpyarabot.config import (CALLBACK_ACCEPT, CALLBACK_CANCEL_REQUEST,
                                CALLBACK_REJECT, CALLBACK_SUBMIT_ACCEPTANCE,
                                CALLBACK_TOGGLE_ALT, CALLBACK_TOGGLE_FORCE,
                                CALLBACK_TOGGLE_PRIVDUMP)
from dumpyarabot.schemas import AcceptOptionsState


# Message Templates
SUBMISSION_TEMPLATE = " Request submitted for review: {url}"
ACCEPTANCE_TEMPLATE = " Your request has been accepted and processing started"
REJECTION_TEMPLATE = " Your request was rejected: {reason}"
REVIEW_TEMPLATE = (
    " New dump request from @{username}\n"
    "URL: {url}\n"
    "Request ID: {request_id}\n\n"
    " Original message:\n"
    "{original_message}"
)


def create_review_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Create Accept/Reject/Cancel buttons with callback data."""
    keyboard = [
        [
            InlineKeyboardButton(
                " Accept", callback_data=f"{CALLBACK_ACCEPT}{request_id}"
            ),
            InlineKeyboardButton(
                " Reject", callback_data=f"{CALLBACK_REJECT}{request_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                " Cancel Request",
                callback_data=f"{CALLBACK_CANCEL_REQUEST}{request_id}",
            )
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def create_options_keyboard(
    request_id: str, current_state: AcceptOptionsState
) -> InlineKeyboardMarkup:
    """Create toggle buttons for each option + Submit button with current state checkmarks."""

    # Create toggle buttons with checkmarks for enabled options
    alt_text = f"{'YES ' if current_state.alt else 'NO '}Alternative Dumper"
    force_text = f"{'YES ' if current_state.force else 'NO '}Force Re-Dump"
    privdump_text = f"{'YES ' if current_state.privdump else 'NO '}Private Dump"

    keyboard = [
        [
            InlineKeyboardButton(
                alt_text, callback_data=f"{CALLBACK_TOGGLE_ALT}{request_id}"
            )
        ],
        [
            InlineKeyboardButton(
                force_text, callback_data=f"{CALLBACK_TOGGLE_FORCE}{request_id}"
            )
        ],
        [
            InlineKeyboardButton(
                privdump_text, callback_data=f"{CALLBACK_TOGGLE_PRIVDUMP}{request_id}"
            )
        ],
        [
            InlineKeyboardButton(
                " Submit", callback_data=f"{CALLBACK_SUBMIT_ACCEPTANCE}{request_id}"
            )
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def create_submission_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Create Cancel button for submission confirmation message."""
    keyboard = [
        [
            InlineKeyboardButton(
                " Cancel Request",
                callback_data=f"{CALLBACK_CANCEL_REQUEST}{request_id}",
            )
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


