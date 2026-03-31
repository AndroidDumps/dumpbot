"""URL validation and normalization utilities."""

from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx
from pydantic import AnyHttpUrl, ValidationError


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
        # Use existing Pydantic validation
        validated_url = AnyHttpUrl(url_str)
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
        async with httpx.AsyncClient(verify=False) as client:
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