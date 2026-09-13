"""URL validation and normalization utilities."""

import re
from typing import Optional, Tuple
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError


HTTP_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
URL_TOKEN_PATTERN = re.compile(r"^https?://\S+$", re.IGNORECASE)


def parse_moderated_request(message_text: str) -> tuple[str, list[str]]:
    """Extract free text and the consecutive URL tokens consumed by #request."""
    match = re.search(r"#request", message_text, re.IGNORECASE)
    if not match:
        return "", []

    prefix_tokens = message_text[: match.start()].strip().split()
    tail_tokens = message_text[match.end() :].strip().split()
    between_tokens: list[str] = []
    urls: list[str] = []
    index = 0

    while index < len(tail_tokens) and not URL_TOKEN_PATTERN.fullmatch(tail_tokens[index]):
        between_tokens.append(tail_tokens[index])
        index += 1

    while index < len(tail_tokens) and URL_TOKEN_PATTERN.fullmatch(tail_tokens[index]):
        urls.append(tail_tokens[index])
        index += 1

    suffix_tokens = tail_tokens[index:]
    message_tokens = [*prefix_tokens, *between_tokens, *suffix_tokens]
    return " ".join(message_tokens).strip(), urls


def parse_dump_tokens(tokens: list[str]) -> tuple[list[str], str]:
    """Split ordered firmware URLs from an optional trailing mode token."""
    values = list(tokens)
    options = ""
    while values and values[-1] and set(values[-1].lower()) <= set("afp"):
        options = values.pop().lower() + options
    if not values:
        raise ValueError("At least one firmware URL is required")
    return values, options


def is_whitelisted_url(url: str) -> bool:
    """Check URL hostname against whitelist domains."""
    whitelist_file = Path.home() / "dumpbot" / "whitelist.txt"
    if not whitelist_file.exists():
        return False
    try:
        with whitelist_file.open("r", encoding="utf-8") as handle:
            domains = [line.strip().lower() for line in handle if line.strip()]
    except OSError:
        return False

    hostname = (urlparse(url).hostname or "").lower()
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains)


async def validate_and_normalize_url(url_str: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validate URL and return (is_valid, normalized_url, error_message).

    Args:
        url_str: The URL string to validate

    Returns:
        Tuple of (is_valid, normalized_url, error_message)
        - is_valid: Whether the URL is valid
        - normalized_url: The normalized URL string if valid, None otherwise
        - error_message: Error description if invalid, None otherwise
    """
    try:
        validated_url = HTTP_URL_ADAPTER.validate_python(url_str)
        return True, str(validated_url), None
    except ValidationError as e:
        return False, None, f"Invalid URL: {e}"


async def check_url_accessibility(url: str, timeout: int = 10) -> bool:
    """
    Check if URL is accessible.

    Args:
        url: The URL to check
        timeout: Request timeout in seconds

    Returns:
        True if URL is accessible (status code < 400), False otherwise
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.head(url, timeout=timeout, follow_redirects=True)
            return response.status_code < 400
    except Exception:
        return False


def parse_url_components(url: str) -> Optional[tuple[str, str, str]]:
    """
    Parse URL into its main components.

    Args:
        url: The URL to parse

    Returns:
        Tuple of (scheme, netloc, path) if valid, None otherwise
    """
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None
        return parsed.scheme, parsed.netloc, parsed.path
    except Exception:
        return None


async def validate_firmware_url(url_str: str, check_accessibility: bool = True) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Complete firmware URL validation including format and accessibility checks.

    Args:
        url_str: The URL string to validate
        check_accessibility: Whether to also check if URL is accessible

    Returns:
        Tuple of (is_valid, normalized_url, error_message)
    """
    # First validate URL format
    is_valid, normalized_url, error_msg = await validate_and_normalize_url(url_str)

    if not is_valid:
        return False, None, error_msg

    # Optionally check accessibility
    if check_accessibility and normalized_url:
        is_accessible = await check_url_accessibility(normalized_url)
        if not is_accessible:
            return False, normalized_url, "URL is not accessible"

    return True, normalized_url, None
