from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from dumpyarabot.schemas import AcceptOptionsState

# Callback data prefixes (duplicated from config to avoid import issues)
CALLBACK_ACCEPT = "accept_"
CALLBACK_REJECT = "reject_"
CALLBACK_TOGGLE_ALT = "toggle_alt_"
CALLBACK_TOGGLE_FORCE = "toggle_force_"
CALLBACK_TOGGLE_PRIVDUMP = "toggle_privdump_"
CALLBACK_SUBMIT_ACCEPTANCE = "submit_accept_"
CALLBACK_CANCEL_REQUEST = "cancel_req_"
CALLBACK_JENKINS_CANCEL = "jenkins_cancel_"


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
    alt_text = f"{'' if current_state.alt else ''} Alternative Dumper"
    force_text = f"{'' if current_state.force else ''} Force Re-Dump"
    privdump_text = f"{'' if current_state.privdump else ''} Private Dump"

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


def create_jenkins_cancel_keyboard(job_name: str, build_id: str) -> InlineKeyboardMarkup:
    """Create Cancel button for Jenkins status messages."""
    keyboard = [
        [
            InlineKeyboardButton(
                f" Cancel {job_name.title()} Job",
                callback_data=f"{CALLBACK_JENKINS_CANCEL}{job_name}:{build_id}",
            )
        ]
    ]
    return InlineKeyboardMarkup(keyboard)
