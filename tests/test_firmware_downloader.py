"""Unit tests for FirmwareDownloader error reporting.

Regression coverage for the "Both aria2 RPC and wget failed. Last error: "
(blank) failure log: the final exception must carry the real aria2 and wget
reasons instead of swallowing them.
"""

from unittest.mock import AsyncMock, patch

import pytest

from dumpyarabot.firmware_downloader import FirmwareDownloader
from dumpyarabot.process_utils import ProcessResult


class _FakeAria2:
    """Async-context-manager stand-in whose download() fails like a real 403."""

    def __init__(self, error: str):
        self._error = error

    def __call__(self, *args, **kwargs):
        # Aria2Manager(...) is constructed inside _download_default; return self
        # so the same configured instance is used as the context manager.
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def download(self, url, **kwargs):
        if False:  # pragma: no cover - makes this an async generator
            yield
        raise RuntimeError(self._error)

    def get_downloaded_file_path(self):
        return None


async def _run_default(tmp_path, aria2_error: str, wget_result: ProcessResult):
    downloader = FirmwareDownloader(str(tmp_path))
    fake_aria2 = _FakeAria2(aria2_error)
    with (
        patch("dumpyarabot.firmware_downloader.Aria2Manager", fake_aria2),
        patch(
            "dumpyarabot.firmware_downloader.run_download_command",
            new=AsyncMock(return_value=wget_result),
        ) as wget_mock,
    ):
        with pytest.raises(Exception) as exc_info:
            await downloader._download_default("https://example.com/fw.zip")
    return str(exc_info.value), wget_mock


async def test_both_failed_message_includes_aria2_and_wget_errors(tmp_path):
    aria2_error = "aria2 download error (code 22): URI not found, status 403"
    wget_result = ProcessResult(
        returncode=8,
        stderr="https://example.com/fw.zip: ERROR 403: Forbidden.",
        command=["wget", "-nv", "https://example.com/fw.zip"],
    )

    message, wget_mock = await _run_default(tmp_path, aria2_error, wget_result)

    # The real reasons from BOTH tools must reach the final exception.
    assert "code 22" in message
    assert "403" in message
    assert "Forbidden" in message
    # And the wget fallback must run with -nv (not -q, which silences errors).
    called_args = wget_mock.await_args.args
    assert "-nv" in called_args
    assert "-q" not in called_args


async def test_empty_wget_stderr_shows_placeholder_not_blank(tmp_path):
    # The original bug: wget -q exits non-zero with empty stderr -> blank message.
    wget_result = ProcessResult(
        returncode=8,
        stderr="",
        command=["wget", "-nv", "https://example.com/fw.zip"],
    )

    message, _ = await _run_default(tmp_path, "boom from aria2", wget_result)

    assert "boom from aria2" in message
    assert "(no stderr output)" in message
    # Must never end with a dangling, empty "Last error: ".
    assert not message.rstrip().endswith(":")
