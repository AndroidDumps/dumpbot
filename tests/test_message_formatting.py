"""Tests for Telegram legacy-Markdown safety in message formatting.

Telegram's legacy ``Markdown`` parse mode has two rules that these formatters
must respect, and getting them backwards breaks real dumps:

* Text **outside** a ``code span`` must have ``* _ ` [`` backslash-escaped, or
  Telegram rejects the message ("can't find end of the entity ...") and the
  edit is dropped to the dead-letter queue — which is why a finished dump's
  status message froze at "92% Sending channel notification".
* Text **inside** a ``code span`` must NOT be escaped: a backslash is literal
  there, so escaping leaks a visible ``\\_`` into channel posts.
"""

import re

from dumpyarabot.message_formatting import (
    format_comprehensive_progress_message,
    format_device_properties_message,
)


def _strip_code_spans_and_escapes(text: str) -> str:
    """Remove backslash-escapes and `code spans` so only "live" markup remains."""
    text = re.sub(r"\\.", "", text)  # drop escaped pairs like \_ \* \[
    text = re.sub(r"`[^`]*`", "", text)  # drop `code spans` (special chars literal)
    return text


def test_device_properties_does_not_backslash_escape_inside_code_spans():
    """Channel post: values live in backticks, so escaping leaks a visible \\_."""
    props = {
        "brand": "motorola",
        "codename": "lagos_g",
        "release": "15",
        "fingerprint": "motorola/lagos_g/lagos:15/VVOB35.78-71-9/ecbb89:user/release-keys",
        "platform": "mt6768",
    }

    msg = format_device_properties_message(props)

    assert "\\_" not in msg, f"backslash leaked into a code span: {msg!r}"
    assert "`motorola/lagos_g/lagos:15/VVOB35.78-71-9/ecbb89:user/release-keys`" in msg


async def test_completion_message_has_no_unescaped_underscore_outside_code_spans():
    """Status edit: repo url + device codename sit outside backticks and must
    be escaped, or Telegram 400s the final 100% edit (the "stuck at 92%" bug)."""
    job_data = {
        "job_id": "29f0844a4fc26d57",
        "worker_id": "arq@29f0844a",
        "dump_args": {"url": "https://example.com/lagos_g_user.zip"},
    }
    progress = {"percentage": 100.0, "current_step_number": 19, "total_steps": 25}
    metadata = {
        "start_time": "2026-05-23T15:28:23+00:00",
        "device_info": {
            "brand": "motorola",
            "codename": "lagos_g",
            "android_version": "15",
        },
        "repository": {
            "url": "https://dumps.tadiphone.dev/dumps/motorola/lagos/tree/"
            "lagos_g-user-15-VVOB35.78-71-9-ecbb89-release-keys/",
        },
    }

    msg = await format_comprehensive_progress_message(
        job_data, "Repository created successfully", progress, metadata
    )

    stripped = _strip_code_spans_and_escapes(msg)
    assert "_" not in stripped, (
        "unescaped underscore outside a code span breaks Telegram Markdown: " f"{msg!r}"
    )
