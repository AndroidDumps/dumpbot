import re
import secrets
import asyncio
from datetime import datetime
from typing import List, Tuple, Optional

import httpx
from rich.console import Console

from dumpyarabot import schemas
from dumpyarabot.config import settings

console = Console()


async def retry_http_request(
    method: str,
    url: str,
    max_retries: int = 3,
    base_delay: float = 2.0,
    **kwargs
) -> httpx.Response:
    """
    Simple retry wrapper for HTTP requests with exponential backoff.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: Request URL
        max_retries: Maximum number of retry attempts
        base_delay: Base delay between retries in seconds
        **kwargs: Additional arguments passed to httpx request
    """
    last_exception = None

    for attempt in range(max_retries + 1):  # +1 for initial attempt
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                return response

        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
            last_exception = e

            if attempt == max_retries:  # Last attempt
                console.print(f"[red]HTTP request failed after {max_retries + 1} attempts: {e}[/red]")
                break

            # Calculate delay with exponential backoff
            delay = base_delay * (2 ** attempt)
            console.print(f"[yellow]Attempt {attempt + 1} failed, retrying in {delay:.1f}s: {e}[/yellow]")
            await asyncio.sleep(delay)

    # If all attempts failed, raise the last exception
    raise last_exception


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram legacy Markdown format.

    Only escapes characters that are special in legacy Markdown:
    *, _, `, [

    Args:
        text: The text to escape

    Returns:
        Text with Markdown special characters escaped
    """
    if not text:
        return text

    # Legacy Markdown only uses these 4 special characters
    return (text.replace("\\", "\\\\")  # Backslash first
            .replace("*", "\\*")
            .replace("_", "\\_")
            .replace("`", "\\`")
            .replace("[", "\\["))


def generate_request_id() -> str:
    """Generate a unique request ID."""
    return secrets.token_hex(4)  # 8-character hex string


# Sentinels used to shield code spans, fenced blocks and backslash escapes while
# rewriting emphasis markers. Control characters won't occur in our status text.
_RICH_STASH_OPEN = "\x00"
_RICH_STASH_CLOSE = "\x01"
_RICH_STASH_RE = re.compile(r"\x00(\d+)\x01")
_RICH_PROTECT_RE = re.compile(
    r"```.*?```"      # fenced code block (pre)
    r"|`[^`]*`"       # inline code span
    r"|\\.",          # backslash escape, e.g. \* \_ \[ \`
    re.DOTALL,
)


def legacy_markdown_to_rich_markdown(text: str) -> str:
    """Convert Telegram legacy ``Markdown`` text into Bot API 10.1 Rich Markdown.

    The bot builds every status string in legacy ``Markdown`` (``*bold*``,
    ``_italic_``, ``code spans`` and ``[links](url)``). Rich Markdown reuses the
    same syntax with one breaking difference: a single ``*x*`` now means *italic*,
    while **bold** requires ``**x**``. Italic (``_x_``), code spans, fenced blocks,
    links and backslash escapes are identical in both dialects, so the only
    rewrite needed is doubling the bold asterisks.

    In valid legacy Markdown every literal asterisk is backslash-escaped (``\\*``),
    so once escapes, code spans and fenced blocks are shielded, every remaining
    ``*`` is a bold delimiter and can be safely doubled.

    Args:
        text: A string formatted for the legacy ``Markdown`` parse mode.

    Returns:
        The equivalent string formatted for the Rich Markdown parse mode.
    """
    if not text:
        return text

    stash: List[str] = []

    def _protect(match: "re.Match[str]") -> str:
        stash.append(match.group(0))
        return f"{_RICH_STASH_OPEN}{len(stash) - 1}{_RICH_STASH_CLOSE}"

    shielded = _RICH_PROTECT_RE.sub(_protect, text)

    # Every surviving asterisk delimits legacy bold; ``**`` is Rich Markdown bold.
    shielded = shielded.replace("*", "**")

    return _RICH_STASH_RE.sub(lambda m: stash[int(m.group(1))], shielded)
