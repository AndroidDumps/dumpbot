"""Tests for the legacy-Markdown -> Bot API 10.1 Rich Markdown converter.

The bot builds every /dump status string in Telegram's legacy ``Markdown``
dialect, where ``*x*`` means bold. Rich Markdown reuses the same syntax but
reads ``*x*`` as *italic* and needs ``**x**`` for bold. The converter must flip
exactly the bold markers while leaving italics, code spans, fenced blocks,
links and backslash escapes untouched.
"""

from dumpyarabot.message_formatting import format_comprehensive_progress_message
from dumpyarabot.utils import legacy_markdown_to_rich_markdown as to_rich


def test_bold_markers_are_doubled():
    assert to_rich("*Job ID:* done") == "**Job ID:** done"


def test_multiple_bold_spans_on_one_line():
    assert to_rich("*URL:* `x` *Options:* a") == "**URL:** `x` **Options:** a"


def test_italic_underscores_are_preserved():
    # ``_x_`` is italic in both dialects and must not change.
    assert to_rich("_italic_ and *bold*") == "_italic_ and **bold**"


def test_asterisks_inside_code_spans_are_not_touched():
    assert to_rich("`a*b*c` *bold*") == "`a*b*c` **bold**"


def test_escaped_asterisk_stays_a_literal_and_is_not_doubled():
    # ``\*`` is a literal asterisk in legacy Markdown; it stays escaped in Rich
    # Markdown and must NOT be turned into a bold delimiter.
    assert to_rich(r"a \* b *bold*") == r"a \* b **bold**"


def test_escaped_underscore_inside_a_bare_url_is_preserved():
    src = r"*Repository:* https://example.com/tree/lagos\_g-user"
    assert to_rich(src) == r"**Repository:** https://example.com/tree/lagos\_g-user"


def test_fenced_code_block_is_left_intact():
    src = "```\n*not bold*\n``` *bold*"
    assert to_rich(src) == "```\n*not bold*\n``` **bold**"


def test_links_are_preserved():
    assert to_rich("[repo](https://t.me/) *bold*") == "[repo](https://t.me/) **bold**"


def test_empty_and_plain_text():
    assert to_rich("") == ""
    assert to_rich("no markup here") == "no markup here"


async def test_converted_progress_message_has_only_double_asterisk_bold():
    """A real status message must contain no lone ``*`` bold delimiters once
    converted (every bold becomes ``**``), so it renders bold under Rich Markdown."""
    job_data = {
        "job_id": "abc123",
        "dump_args": {"url": "https://example.com/fw.zip", "use_alt_dumper": False},
        "worker_id": "arq_worker",
    }
    progress = {"percentage": 45, "current_step_number": 4, "total_steps": 8}

    legacy = await format_comprehensive_progress_message(job_data, "Downloading", progress)
    rich = to_rich(legacy)

    # No single-asterisk bold pairs should survive: every '*' must be part of '**'.
    # Strip code spans first (asterisks there are literal and allowed).
    import re
    stripped = re.sub(r"`[^`]*`", "", rich)
    stripped = re.sub(r"\\.", "", stripped)
    assert "**" in stripped  # bold labels are present
    assert "*" not in stripped.replace("**", "")  # nothing but doubled asterisks
