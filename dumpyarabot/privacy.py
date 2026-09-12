import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
PRIVATE_URL_PLACEHOLDER = "[hidden for private dump]"


def is_private_job(job: Any) -> bool:
    """Return whether a model or persisted job payload is private."""
    if isinstance(job, dict):
        dump_args = job.get("dump_args", job)
        return bool(dump_args.get("use_privdump", False))
    dump_args = getattr(job, "dump_args", job)
    return bool(getattr(dump_args, "use_privdump", False))


def sanitize_url(url_value: Any) -> str:
    """Remove URL credentials and query data for non-private displays."""
    url = str(url_value or "unknown")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname or ""
        port = parts.port
        username = parts.username
    except ValueError:
        return url

    netloc = hostname
    if port:
        netloc = f"{netloc}:{port}"
    if username:
        netloc = f"[REDACTED]@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", "")) or url


def redact_urls(value: Any, *, private: bool) -> str:
    """Replace every URL for private jobs; otherwise remove URL credentials."""
    text = str(value)

    def replace(match: re.Match[str]) -> str:
        suffix = ""
        url = match.group(0)
        while url and url[-1] in ".,;:!?)\"]}":
            suffix = url[-1] + suffix
            url = url[:-1]
        replacement = PRIVATE_URL_PLACEHOLDER if private else sanitize_url(url)
        return replacement + suffix

    return _URL_PATTERN.sub(replace, text)


def redact_for_job(value: Any, job: Any) -> str:
    """Redact URLs and URL-derived download names for a private job."""
    private = is_private_job(job)
    text = redact_urls(value, private=private)
    if not private:
        return text

    dump_args = job.get("dump_args", job) if isinstance(job, dict) else getattr(job, "dump_args", job)
    if isinstance(dump_args, dict):
        urls = [dump_args.get("url"), *(dump_args.get("delta_urls") or [])]
    else:
        urls = [getattr(dump_args, "url", None), *getattr(dump_args, "delta_urls", [])]
    for source_url in filter(None, urls):
        path_name = urlsplit(str(source_url)).path.rpartition("/")[2]
        decoded_path_name = unquote(path_name)
        for source_text in (
            str(source_url),
            path_name,
            decoded_path_name,
            PurePosixPath(path_name).stem if path_name else "",
            PurePosixPath(decoded_path_name).stem if decoded_path_name else "",
        ):
            if source_text:
                text = text.replace(source_text, PRIVATE_URL_PLACEHOLDER)
    return text
